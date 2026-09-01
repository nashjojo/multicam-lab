#!/usr/bin/env python3
"""给 sync_viewer.html 起一个**支持 Range 请求**的本地静态服务器。

为什么不能直接用 ``python3 -m http.server``：它对 ``Range`` 头返回 200 + 全量
（不是 206 + 分片），Chrome 于是把媒体判定为不可 seek —— 表现是视频能播、但
拖动时间轴时画面**完全不动**（``video.seekable`` 为空区间），即使文件已整段缓冲。
本脚本补上 206 分片响应，拖动才真正生效。

用法（默认服务根目录 = 本脚本所在目录，无需先 cd）：

    python3 serve_viewer.py                 # 自动挑最新一次录制的清单并打印 URL
    python3 serve_viewer.py --port 8000
    python3 serve_viewer.py --dir other_clips
    python3 serve_viewer.py --root ..       # 看别处的录制（该目录须有 sync_viewer.html）

本脚本只用标准库，不装依赖也能跑。

不想起服务也行：直接用浏览器打开 sync_viewer.html，把清单 + mp4 一起拖进页面
——那条路径走 blob URL，天然可 seek。
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import socketserver
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + 单区间 Range 支持（够媒体 seek 用）。"""

    protocol_version = "HTTP/1.1"  # keep-alive；配合精确 Content-Length

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()
        m = _RANGE_RE.match(rng.strip())
        if not m:
            return super().do_GET()  # 多区间/畸形：退化成整段，浏览器仍可播

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()
        size = os.path.getsize(path)
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "":
            # "bytes=-N"：文件末尾 N 字节
            length = min(int(end_s or 0), size)
            start, end = size - length, size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        if start >= size or start < 0:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        end = min(end, size - 1)
        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # 浏览器换 Range 时会掐断上一个请求，属正常
                remaining -= len(chunk)

    def end_headers(self):
        # 让浏览器知道可以发 Range（非 206 响应也带上）
        if "Accept-Ranges" not in self._headers_buffer_keys():
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _headers_buffer_keys(self) -> set[str]:
        out = set()
        for raw in getattr(self, "_headers_buffer", []) or []:
            line = raw.decode("latin-1", "ignore")
            if ":" in line:
                out.add(line.split(":", 1)[0])
        return out

    def log_message(self, fmt, *args):  # 静音逐请求日志（媒体请求非常多）
        pass


class Server(socketserver.ThreadingTCPServer):
    """多线程：一个页面会并发拉 4 条视频流，单线程会互相排队。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """对端掐断连接不算错误，别刷一整页 traceback。

        本服务是 HTTP/1.1 keep-alive，而浏览器放媒体时会**频繁**主动断连：拖进度条
        时放弃正在进行的 Range 请求、回收空闲连接、关标签页都会。socketserver 在读
        请求行时收到 ECONNRESET 就把整个栈打出来，一次拖动能刷几十屏，真有问题时
        反而看不见。``do_GET`` 里那处 try 只覆盖写响应体的阶段，覆盖不到这里。

        只吞连接类异常，其余照常打印 —— 否则真 bug 会被静默掉。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    p = argparse.ArgumentParser(description="给 sync_viewer.html 起支持 Range 的本地服务")
    p.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    p.add_argument("--dir", default="sync_clips", help="录制输出目录，默认 sync_clips")
    p.add_argument("--root", default=str(Path(__file__).resolve().parent),
                   help="服务根目录（须含 sync_viewer.html），默认本脚本所在目录")
    args = p.parse_args()

    root = Path(args.root).resolve()
    if not (root / "sync_viewer.html").exists():
        print(f"[错误] {root}/sync_viewer.html 不存在。该文件须与本脚本同目录，"
              f"或用 --root 指定它所在的目录。", file=sys.stderr)
        sys.exit(1)

    # 新布局：一次录制一个文件夹，清单固定叫 manifest.json；
    # 旧布局：平链在目录里、带时间戳前缀。两者都找，按修改时间取最新。
    manifests = sorted(
        glob.glob(str(root / args.dir / "*" / "manifest.json"))
        + glob.glob(str(root / args.dir / "*_manifest.json")),
        key=lambda p: os.path.getmtime(p),
    )
    url = f"http://127.0.0.1:{args.port}/sync_viewer.html"
    if manifests:
        rel = Path(manifests[-1]).relative_to(root).as_posix()
        url += f"?m={rel}"
        print(f"最新清单：{rel}（共 {len(manifests)} 份，可在页面里换）")
    else:
        print(f"[提示] {args.dir}/ 下没找到清单，先跑 multi_cam_recorder.py 录一段；"
              f"也可在页面里手动选文件。")

    print(f"\n  打开：{url}\n\nCtrl+C 停止。")
    os.chdir(root)
    with Server(("127.0.0.1", args.port), partial(RangeHandler, directory=str(root))) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


if __name__ == "__main__":
    main()
