#!/usr/bin/env python3
"""录制会话：一键起服务、按空格反复录段、后台自动校验 + 预览。

交互
----
    space   开始录制 / 录制中按下则提前结束（保留已录内容）/ 结束后再按录下一段
    v       打开/打印当前最新一段的预览地址
    q       退出（会等在录的那段收尾）

设计原则：**录制优先**
------------------
校验与预览一律在后台线程里跑，绝不挡住下一次按键 —— 拍摄现场最怕的是"想录的
瞬间工具在忙"。所以：

* 每段录完立刻把校验任务丢进后台队列，主循环马上回到"待命"状态；
* 预览服务在会话开始时起一次、常驻，不随录制起停；
* 后台任务失败只打一行提示，不影响录制。

依赖与启动
----------
依赖由本子库的 ``pyproject.toml`` 声明，首次先建环境：

    uv sync

之后直接起会话（等价于 ``./rec_session.sh``）：

    uv run rec_session.py

默认只录**本机 USB 摄像头**。米家摄像头是可选项，要一起录得显式加 ``--miot``
（需 miloco 后端可达）。

常用参数：``--seconds`` 单段上限（默认 60）、``--no-verify`` 关掉校验、
``--no-serve`` 不起预览服务、``--no-voice`` 不播语音提示。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
RECORDER = REPO / "multi_cam_recorder.py"
VIEWER = REPO / "sync_viewer.html"
SERVER = REPO / "serve_viewer.py"

# 录制起步阶段的标记（由 multi_cam_recorder 在栅栏放行时打印，带 flush）。
# 靠它判断"真正开始录了"，用于语音报幕与计时 —— 不能拿进程启动时刻当开始，
# 前面还有 USB 预热和设备打开好几秒。
START_MARK = "栅栏已放行"


# ── 终端按键 ────────────────────────────────────────────────────────────────


class RawKeys:
    """把终端切到 raw 模式，非阻塞读单键；退出时恢复。

    用 termios 而不是第三方库：零依赖，且这脚本本来就只在 macOS/Linux 跑。
    非 tty（被重定向）时退化为"不读键"，避免直接崩。
    """

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else -1
        self.saved = None
        self.eof = False        # 终端已到 EOF（对端关闭），不会再有按键

    def __enter__(self) -> "RawKeys":
        if self.fd >= 0:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *_exc) -> None:
        if self.fd >= 0 and self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def get(self, timeout: float = 0.2) -> str | None:
        """等一个键；超时返回 None。"""
        if self.fd < 0 or self.eof:
            time.sleep(timeout)
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if ch == "":
            # tty 到了 EOF（典型场景：被 pty 驱动时对端关闭、终端窗口被关）。此后
            # ``select`` 会**永远**报可读、``read`` 永远返空串：若当成「没按键」继续轮
            # 询，主循环就变成没有任何 sleep 的死转——实测一个被中断的测试会话就这样
            # 以 98% CPU 跑了 19 小时。标记下来让会话正常收摊。
            self.eof = True
            time.sleep(timeout)
            return None
        return ch

    def flush(self) -> None:
        """丢掉还没读的输入。

        "准备摄像头"要好几秒，等不及的人会连按几下 space —— 这些键留在终端缓冲
        里，等录制循环一开就被读到，表现为"刚开录就自己结束了"。故开录前清一次。
        """
        if self.fd >= 0:
            termios.tcflush(self.fd, termios.TCIFLUSH)


# ── 语音提示 ────────────────────────────────────────────────────────────────


def say(text: str, enabled: bool = True, wait: bool = False) -> None:
    """中文语音提示。

    固定用 ``Tingting``：它是本机唯一的中文专属语音名。像 ``Flo`` 这类名字在
    多个语言下都存在，``say -v Flo`` 会挑到第一个匹配项（可能是英语语音去念
    中文），听起来完全不对 —— 实测踩过。
    """
    if not enabled:
        return
    cmd = ["say", "-v", "Tingting", text]
    try:
        if wait:
            subprocess.run(cmd, check=False, timeout=20)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        pass                    # 没有 say / 无音频设备：静默降级


def terminate(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """请子进程收尾，宽限期内不退就强杀 —— 绝不无限等。

    录制进程卡住（设备掉线、WS 不回包）时若死等，整个会话就废了；拍摄现场宁可
    丢掉这一段，也要把控制权交回给按键。
    """
    if proc.poll() is None:
        proc.terminate()
    for t in (grace, 5):
        try:
            proc.wait(timeout=t)
            return
        except subprocess.TimeoutExpired:
            proc.kill()


# ── 后台任务（校验、预览地址）────────────────────────────────────────────────


def verify_clip(clip_dir: Path) -> str:
    """校验一段录制，返回一行结论。**在后台线程跑，不得阻塞主循环。**

    校验内容：清单可读、每路视频可解码且时长合理、USB 帧率比自洽、音频丢包数。
    解码用 PyAV；缺依赖时给出提示而不是抛栈。
    """
    try:
        import av
    except ImportError:
        return f"[校验] {clip_dir.name}: 跳过（缺 av 依赖）"
    try:
        m = json.loads((clip_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception as e:                      # noqa: BLE001
        return f"[校验] {clip_dir.name}: 读不到清单 — {e}"

    want = float(m.get("duration_s") or 0)
    bad, notes = [], []
    for s in m.get("sources") or []:
        if s.get("error") or not s.get("file"):
            bad.append(f"{s.get('label', '?')[:16]}:{s.get('error') or '无文件'}")
            continue
        p = clip_dir / s["file"]
        try:
            c = av.open(str(p))
            n = sum(1 for pk in c.demux(video=0) for _ in pk.decode())
            dur = (c.duration or 0) / 1e6
        except Exception as e:                  # noqa: BLE001
            bad.append(f"{p.name}:解码失败({type(e).__name__})")
            continue
        if n < 5:
            bad.append(f"{p.name}:仅 {n} 帧")
        # 提前结束时实际时长必然短于请求值，只报"比请求长"或"异常短"
        elif want and (dur > want * 1.15 or dur < 0.5):
            bad.append(f"{p.name}:时长 {dur:.1f}s(请求 {want:.0f}s)")

    u = next((s for s in (m.get("sources") or []) if s.get("kind") == "usb"), None)
    if u and u.get("fps_written") and u.get("fps_actual"):
        expect = u["fps_written"] / u["fps_actual"]
        got = u.get("playback_speed_ratio") or 0
        if abs(expect - got) > 0.002:
            bad.append(f"USB 速度比不自洽({got} vs {expect:.4f})")

    for a in m.get("audio") or []:
        if a.get("error"):
            notes.append(f"{a['label'][:14]} 无音频")
        elif a.get("seq_gaps"):
            notes.append(f"{a['label'][:14]} 丢包 {a['seq_gaps']}")

    off = (m.get("alignment") or {}).get("max_offset_ms")
    head = f"[校验] {clip_dir.name}"
    if bad:
        return f"{head}: ✗ " + "; ".join(bad[:4])
    tail = f"，{'; '.join(notes)}" if notes else ""
    return (f"{head}: ✓ {len(m.get('sources') or [])} 路，"
            f"最大起点差 {off:.0f}ms{tail}"
            if off is not None else f"{head}: ✓{tail}")


class Background:
    """单线程后台队列：任务串行跑，主循环只管投递。

    串行而非并发：校验要解码整段视频，几路并发会跟下一次录制抢 CPU，
    而"录制优先"是本工具的第一原则。
    """

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.out: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self) -> None:
        while True:
            fn = self.q.get()
            if fn is None:
                return
            try:
                msg = fn()
                if msg:
                    self.out.put(msg)
            except Exception as e:              # noqa: BLE001
                self.out.put(f"[后台] 任务失败: {type(e).__name__}: {e}")

    def submit(self, fn) -> None:
        self.q.put(fn)

    def drain(self) -> list[str]:
        msgs = []
        while True:
            try:
                msgs.append(self.out.get_nowait())
            except queue.Empty:
                return msgs


# ── 预览服务 ────────────────────────────────────────────────────────────────


def viewer_alive(port: int) -> bool:
    """该端口上是不是已经有一个能提供回放页的服务。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/sync_viewer.html", timeout=2) as r:
            return r.status == 200
    except Exception:                            # noqa: BLE001
        return False


def start_server(port: int, out_dir: str) -> tuple[subprocess.Popen | None, bool]:
    """起常驻预览服务（支持 Range，拖进度条才有效）。

    返回 ``(自己起的进程, 服务是否可用)``。会话开始起一次、全程复用；不随录制
    起停 —— 每段录完只需换 URL 里的清单路径。

    端口已被占时先探一下：若那头就是一个能提供回放页的服务（常见于上一次会话
    没退干净），直接复用 —— 不必报错也不必换端口，更不能去杀别人的进程。

    服务根目录用 ``--root`` 显式钉在脚本目录（``sync_viewer.html`` 在那儿），
    不靠继承 CWD —— 本会话可能从任意目录启动。
    """
    try:
        p = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(port),
             "--root", str(REPO), "--dir", out_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:                      # noqa: BLE001
        print(f"[警告] 预览服务起不来：{e}", file=sys.stderr)
        return None, False
    time.sleep(1.2)
    if p.poll() is None:
        return p, True
    if viewer_alive(port):
        print(f"[提示] 端口 {port} 上已有预览服务，直接复用。")
        return None, True
    print(f"[警告] 预览服务起不来（端口 {port} 被占用？），可用 --port 换端口",
          file=sys.stderr)
    return None, False


def preview_url(port: int, clip_dir: Path) -> str:
    """回放页地址；clip 不在服务根目录下时给出替代办法而不是一条打不开的 URL。"""
    if not clip_dir.is_relative_to(REPO):
        return (f"{clip_dir}（不在 {REPO} 下，常驻预览服务看不到；"
                f"另起 serve_viewer.py --root {clip_dir.parent.parent} 看）")
    rel = clip_dir.relative_to(REPO).as_posix()
    return f"http://127.0.0.1:{port}/sync_viewer.html?m={rel}/manifest.json"


# ── 一段录制 ────────────────────────────────────────────────────────────────


def source_args(args) -> list[str]:
    """把源选择参数透传给 multi_cam_recorder。

    源的写法与底层工具保持一致（``--miot-all`` / 可重复的 ``--usb N`` /
    ``--usb-auto``），避免两套心智模型。
    """
    out: list[str] = []
    if args.miot:
        out.append("--miot-all")
    for i in args.usb:
        out += ["--usb", str(i)]
    if args.usb_auto:
        out.append("--usb-auto")
    return out


def record_one(args, bg: Background, port: int, keys: RawKeys) -> Path | None:
    """录一段：起子进程 → 等真正开始 → 等按键提前结束或自然结束。

    返回 clip 目录；失败返回 None。``keys`` 由主循环持有并传入，全程只切一次
    终端模式（反复切容易把按键吞掉）。
    """
    out_root = Path(args.out)
    stop_file = out_root / f".stop_{os.getpid()}_{int(time.time())}"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    log = out_root / f".rec_{datetime.now():%H%M%S}.log"

    cmd = [sys.executable, str(RECORDER),
           "--seconds", str(args.seconds), "--out", str(out_root),
           "--stop-file", str(stop_file)]
    cmd += source_args(args)
    if args.audio:
        cmd.append("--audio")
    env = dict(os.environ, PYTHONUNBUFFERED="1")

    before = {p.name for p in out_root.iterdir() if p.is_dir()}
    with open(log, "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=str(REPO), env=env)

    # 等"真正开始"（前面有 USB 预热 + 设备打开数秒）
    print("  准备摄像头…", end="", flush=True)
    t0 = time.time()
    started = False
    while time.time() - t0 < 120 and proc.poll() is None:
        try:
            if START_MARK in log.read_text(errors="ignore"):
                started = True
                break
        except OSError:
            pass
        time.sleep(0.15)
    if not started:
        terminate(proc)
        print(f"\r  [失败] 录制没能开始，日志见 {log}")
        return None

    t_start = time.time()
    keys.flush()            # 清掉"准备中"期间的误触，否则一开录就被立刻结束
    say("开始", args.voice)
    print(f"\r  \033[7m● 录制中\033[0m  再按 space 提前结束"
          f"（上限 {args.seconds:g}s）        ", flush=True)

    # 录制期间：只响应 space（提前结束）；其余按键忽略，避免误触打断拍摄
    while proc.poll() is None:
        k = keys.get(0.2)
        el = time.time() - t_start
        if k == " ":
            stop_file.touch()
            print(f"\r  ⏹ {el:.1f}s 处提前结束，收尾中…        ", flush=True)
            break
        print(f"\r  ● {el:5.1f}s / {args.seconds:g}s ", end="", flush=True)

    # 收尾：等子进程真正退出（写尾帧 + 落盘 + 写清单），超时兜底
    deadline = time.time() + 90
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        print("\r  [警告] 收尾超时，强制结束录制进程", flush=True)
        terminate(proc)
    print()

    after = {p.name for p in out_root.iterdir() if p.is_dir()}
    new = sorted(after - before)
    try:
        stop_file.unlink()
    except OSError:
        pass
    if not new:
        print(f"  [失败] 没有产出 clip 目录，日志见 {log}")
        return None
    clip = out_root / new[-1]

    # 摘要直接从子进程日志里捞（它已经打得很清楚，不重复实现）
    try:
        for line in log.read_text(errors="ignore").splitlines():
            if line.lstrip().startswith(("✓", "✗", "♫")) or "对齐：" in line:
                print("   " + line.strip())
    except OSError:
        pass
    try:
        log.unlink()
    except OSError:
        pass

    say("完成", args.voice)
    if args.verify:
        bg.submit(lambda c=clip: verify_clip(c))       # 后台校验，不挡下一段
    print(f"   预览: {preview_url(port, clip)}")
    return clip


# ── 主循环 ──────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="录制会话（space 反复录段）")
    p.add_argument("--out", default=str(REPO / "sync_clips"),
                   help="输出根目录，默认脚本所在目录下的 sync_clips/")
    p.add_argument("--usb", action="append", type=int, default=[], metavar="INDEX",
                   help="本机摄像头索引（`multi_cam_recorder.py --list` 可查）；可重复")
    p.add_argument("--usb-auto", action="store_true",
                   help="自动挑本机摄像头（外置 + 内置，跳过 iPhone/iPad 连续互通）")
    p.add_argument("--miot", dest="miot", action="store_true", default=None,
                   help="额外拉全部米家摄像头通道（需 miloco 后端可达）；默认不带")
    p.add_argument("--no-miot", dest="miot", action="store_false",
                   help="显式不拉米家（默认行为，写脚本时用得上）")
    p.add_argument("--seconds", type=float, default=60,
                   help="单段上限秒数，默认 60；带 --miot 时受后端限制不得超过 60")
    p.add_argument("--audio", action="store_true", default=True,
                   help="同时录音（默认开）")
    p.add_argument("--no-audio", dest="audio", action="store_false")
    p.add_argument("--verify", action="store_true", default=True,
                   help="每段录完后台校验（默认开）")
    p.add_argument("--no-verify", dest="verify", action="store_false")
    p.add_argument("--serve", action="store_true", default=True,
                   help="起常驻预览服务（默认开）")
    p.add_argument("--no-serve", dest="serve", action="store_false")
    p.add_argument("--voice", action="store_true", default=True,
                   help="语音提示（默认开）")
    p.add_argument("--no-voice", dest="voice", action="store_false")
    p.add_argument("--port", type=int, default=8913, help="预览端口，默认 8913")
    args = p.parse_args()

    # 源的默认解析：一个都没指定 → 只自动挑本机 USB 摄像头。米家是可选源，
    # 要一起录得显式加 --miot —— 不在米家局域网（或本机没跑过 miloco 后端）时，
    # 默认带上只会换来一串 503 等待。
    if not args.usb and not args.usb_auto and args.miot is None:
        args.usb_auto = True
    if args.miot is None:
        args.miot = False
    if not (args.miot or args.usb or args.usb_auto):
        print("[错误] 没有任何录制源（--miot / --usb N / --usb-auto 至少给一个）",
              file=sys.stderr)
        sys.exit(1)

    if not RECORDER.exists():
        print(f"[错误] 找不到 {RECORDER}", file=sys.stderr)
        sys.exit(1)

    bg = Background()
    srv, serving = start_server(args.port, args.out) if args.serve else (None, False)

    src_desc = " + ".join(
        (["米家全部通道"] if args.miot else [])
        + ([f"usb[{i}]" for i in args.usb])
        + (["自动挑 USB"] if args.usb_auto else []))
    print("\n" + "=" * 62)
    print("  录制会话就绪")
    print(f"    space  开始录制（上限 {args.seconds:g}s）/ 录制中按下=提前结束")
    print("    v      打印最新一段的预览地址")
    print("    q      退出")
    print(f"    源     {src_desc}")
    print(f"    输出   {args.out}/<时间戳>/    "
          f"{'含音频' if args.audio else '仅视频'}"
          f"{'，后台校验' if args.verify else ''}"
          f"{f'，预览 :{args.port}' if serving else ''}")
    print("=" * 62 + "\n")
    say("录制会话就绪，按空格开始", args.voice)

    last: Path | None = None
    n = 0
    try:
        with RawKeys() as keys:          # 整个会话只切一次终端模式
            while True:
                for msg in bg.drain():
                    print(msg)
                k = keys.get(0.3)
                if keys.eof:
                    # 没有输入源了（终端关闭 / 被脚本驱动完就走），再循环下去只是白烧 CPU。
                    print("\n输入已关闭（stdin EOF），结束会话。")
                    break
                if k is None:
                    continue
                if k == " ":
                    n += 1
                    print(f"— 第 {n} 段 —")
                    clip = record_one(args, bg, args.port, keys)
                    if clip:
                        last = clip
                    print("\n（space 录下一段 · v 预览 · q 退出）")
                elif k in ("v", "V"):
                    if last:
                        print(f"  预览: {preview_url(args.port, last)}")
                    else:
                        print("  还没有录过。")
                elif k in ("q", "Q", "\x03"):        # q / Ctrl-C
                    break
    except KeyboardInterrupt:
        pass
    finally:
        # 退出前把后台校验结果尽量收完，别让用户白等
        deadline = time.time() + 20
        while time.time() < deadline and not bg.q.empty():
            for msg in bg.drain():
                print(msg)
            time.sleep(0.3)
        for msg in bg.drain():
            print(msg)
        if srv:
            srv.terminate()
        print(f"\n共录 {n} 段，输出在 {args.out}/。再见。")
        say("录制会话结束", args.voice, wait=True)


if __name__ == "__main__":
    main()
