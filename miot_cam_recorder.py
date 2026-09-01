#!/usr/bin/env python3
"""米家摄像头录视频小工具（走 miloco 后端 /api/miot/record_clip，纯标准库）。

后端单段录制上限 60 秒；本工具循环调用实现更长录制，**每段落一个 mp4 文件**
（段之间有短暂间隙、文件不拼接——要连续画面请在浏览器开 /api/miot/watch 直看）。

    # 1) 列出米家摄像头（含 云端/局域网/镜头/感知启用 状态）
    python3 miot_cam_recorder.py --list

    # 2) 录一段 60 秒（自动挑第一台可用摄像头）
    python3 miot_cam_recorder.py

    # 3) 指定摄像头（--list 的序号或 did）、通道、单段时长、总时长、输出
    python3 miot_cam_recorder.py --camera 0 --channel 1 \
        --seconds 30 --total-seconds 300 --out ./clips/caseA

后端地址与 Bearer Token 默认读 ``$MILOCO_HOME/config.json``（未设 MILOCO_HOME
则 ``~/.openclaw/miloco``），与 miloco-cli 同源；可用 ``--url`` / ``--token``
或环境变量 ``MILOCO_URL`` / ``MILOCO_TOKEN`` 覆盖。
"""

from __future__ import (
    annotations,  # 系统 python3 可能只有 3.9：注解延迟求值后 str | None 写法才合法
)

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:1810"
SEG_MIN_S, SEG_MAX_S = 2, 60  # 后端 record_clip 的 duration_ms 合法区间（秒）
HTTP_TIMEOUT_GRACE_S = 20  # 客户端超时 = 段时长 + 此余量（后端内部自身留了 +8s）

EXIT_ARG = 1  # 参数 / 环境错误
EXIT_NET = 2  # 连不上后端（与 miloco-cli 的退出码约定一致）
EXIT_API = 3  # 后端业务错误（非 2xx / code != 0）


class ApiError(Exception):
    """后端返回非 2xx。status 保留给调用方分类提示。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ConnectError(Exception):
    """连不上后端。"""


# ─── 配置与服务端调用 ────────────────────────────────────────────────────────


def load_server_config(url_arg: str | None, token_arg: str | None,
                       required: bool = True) -> tuple[str, str]:
    """确定后端地址与 token：命令行 > 环境变量 > $MILOCO_HOME/config.json > 默认。

    ``required=False`` 时拿不到 token 就返回空串，由调用方决定降级还是报错 ——
    米家摄像头对多机位录制是可选源，没装过 miloco 的机器上不该因此走不下去。
    """
    url = url_arg or os.environ.get("MILOCO_URL")
    token = token_arg or os.environ.get("MILOCO_TOKEN")
    if url and token:
        return url.rstrip("/"), token

    home = Path(os.environ.get("MILOCO_HOME") or Path.home() / ".openclaw" / "miloco")
    cfg_path = home / "config.json"
    if not url or not token:
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            server = cfg.get("server") or {}
            url = url or server.get("url")
            token = token or server.get("token")
        except (OSError, json.JSONDecodeError):
            pass
    url = url or DEFAULT_URL

    if not token and required:
        print(
            f"[错误] 未找到 Bearer Token（{cfg_path} 里没有 server.token，"
            f"也没设 MILOCO_TOKEN / --token）。后端首次启动时会生成 token，"
            f"请确认 miloco 后端已在本机跑过至少一次。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ARG)
    return url.rstrip("/"), token or ""


def _request(
    url: str,
    token: str,
    method: str = "GET",
    params: dict | None = None,
    timeout: float = 30,
    json_body: dict | None = None,
) -> urllib.request.urlopen:
    """发请求，网络层失败统一抛 ConnectError。"""
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    elif method == "POST":
        data = b""
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, _http_error_message(e)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectError(str(e)) from e


def _http_error_message(e: urllib.error.HTTPError) -> str:
    """尽量从错误体里挖出人类可读的信息（信封 message / detail 都试，兜底原文）。"""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("message", "detail", "msg"):
                if data.get(key):
                    return str(data[key])
            return body[:200] or str(e)
    except (json.JSONDecodeError, ValueError):
        pass
    return body[:200] or str(e)


def fetch_cameras(url: str, token: str) -> list[dict]:
    """GET /api/miot/scope/cameras，返回逐通道的相机行（snake_case 字段）。"""
    with _request(f"{url}/api/miot/scope/cameras", token) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("code", 0) != 0:
        raise ApiError(200, json.dumps(data, ensure_ascii=False)[:300])
    rows = data.get("data") or []
    if not isinstance(rows, list):
        raise ApiError(200, "camera list response has no data array")
    return rows


def record_clip_with_headers(
    url: str, token: str, did: str, channel: int, seconds: float,
    clip_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """POST /api/miot/record_clip，返回 (mp4 字节, 响应头)。

    多机同录对齐需要 ``X-Clip-First-Frame-Unix-Ms``（首帧到达后端的主机墙钟），
    故单独开一个带头版本；``record_clip`` 委派到它。

    ``clip_id`` 传了就能用 ``stop_clip()`` 提前收尾而不丢素材。
    """
    params = {
        "camera_id": did,
        "channel": channel,
        "duration_ms": int(seconds * 1000),
    }
    if clip_id:
        params["clip_id"] = clip_id
    with _request(
        f"{url}/api/miot/record_clip",
        token,
        method="POST",
        params=params,
        timeout=seconds + HTTP_TIMEOUT_GRACE_S,
    ) as resp:
        return resp.read(), dict(resp.headers)


def stop_clip(url: str, token: str, clip_id: str) -> bool:
    """让后端提前收尾某片段（保留已录内容）；返回是否真的收了。

    不能靠“断开 HTTP 连接”代替：那会让后端请求协程被取消、走 cancel 分支，
    整段素材全丢。404（片段已结束）当成“无需停”处理，不当错。
    """
    try:
        with _request(
            f"{url}/api/miot/record_clip/stop",
            token,
            method="POST",
            params={"clip_id": clip_id},
            timeout=10,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool((data.get("data") or {}).get("stopped"))
    except ApiError as e:
        if e.status == 404:
            return False
        raise


def record_clip(url: str, token: str, did: str, channel: int, seconds: float) -> bytes:
    """POST /api/miot/record_clip，返回 mp4 字节。"""
    mp4, _ = record_clip_with_headers(url, token, did, channel, seconds)
    return mp4


def set_camera_in_use(url: str, token: str, feed_did: str, in_use: bool) -> None:
    """PUT /api/miot/scope/cameras，切换某通道的感知启用（与家庭面板开关同一入口）。"""
    with _request(
        f"{url}/api/miot/scope/cameras",
        token,
        method="PUT",
        json_body={"items": [{"did": feed_did, "in_use": in_use}]},
    ) as resp:
        resp.read()


# ─── 摄像头选择 ──────────────────────────────────────────────────────────────


def _mark(v: bool | None) -> str:
    """三态布尔转可读标记：✓ / ✗ / ?（None=未知，如无镜头开关属性的机型）。"""
    if v is None:
        return "?"
    return "✓" if v else "✗"


def print_camera_list(cameras: list[dict], url: str) -> None:
    print(f"米家摄像头列表（GET {url}/api/miot/scope/cameras）：")
    for i, c in enumerate(cameras):
        name = c.get("name") or "(未命名)"
        room = f"（{c['room_name']}）" if c.get("room_name") else ""
        cc = c.get("channel_count") or 1
        print(
            f"  [{i}] {name}{room}  did={c.get('did')} "
            f"通道 {c.get('channel')}/{cc - 1}  "
            f"云端{_mark(c.get('cloud_online'))} "
            f"局域网{_mark(c.get('lan_reachable'))} "
            f"镜头{_mark(c.get('awake'))} "
            f"感知启用{'✓' if c.get('in_use') else '✗'}"
        )
    print("说明：多目相机每个镜头一行；「镜头✗」= 物理隐私遮挡，需先在米家 App 打开。")
    print("录制用 --camera 指定序号或 did，--channel 指定通道（多目相机）。")


def _pick_auto(cameras: list[dict]) -> dict:
    """自动挑一台「最可能录得到」的：感知启用 > 三态齐好 > 云端+局域网 > 任意。"""
    def score(c: dict) -> tuple[int, ...]:
        return (
            1 if c.get("in_use") else 0,
            1 if c.get("cloud_online") and c.get("lan_reachable") else 0,
            1 if c.get("awake") is not False else 0,
        )

    return max(cameras, key=score)


def resolve_camera(
    cameras: list[dict], camera_arg: str | None, channel_arg: int | None
) -> dict:
    """把 --camera / --channel 解析成一行相机记录。

    --camera 优先按 did 匹配（物理 did 或多目相机的合成 did ``xxx:ch{n}``），
    匹配不上再按 --list 显示的序号；都不中则报错列出可选项。did 是纯数字串，
    与个位数序号天然不冲突。
    """
    if camera_arg is None:
        picked = _pick_auto(cameras)
        print(
            f"自动选择摄像头 [{cameras.index(picked)}] "
            f"{picked.get('name') or '(未命名)'}，did={picked.get('did')}，"
            f"通道 {picked.get('channel')}。不确定的话先跑 --list 看看。"
        )
        return picked

    # 1) 精确匹配 did（合成 did 命中即同时确定了通道）
    for c in cameras:
        if camera_arg == c.get("did") and (":ch" in camera_arg or channel_arg is None):
            return c
    # 2) 物理 did（多目相机可能多行，取显式 --channel 或默认通道 0）
    want_ch = channel_arg if channel_arg is not None else 0
    for c in cameras:
        if camera_arg == c.get("did") and c.get("channel") == want_ch:
            return c
    # 3) 序号（--list 的行号）
    if camera_arg.isdigit():
        idx = int(camera_arg)
        if 0 <= idx < len(cameras):
            return cameras[idx]

    print(f"[错误] 找不到摄像头 {camera_arg!r}。可用项：", file=sys.stderr)
    for i, c in enumerate(cameras):
        print(
            f"  [{i}] did={c.get('did')} 通道 {c.get('channel')} "
            f"{c.get('name') or '(未命名)'}",
            file=sys.stderr,
        )
    sys.exit(EXIT_ARG)


# ─── 录制主流程 ──────────────────────────────────────────────────────────────


def _segment_paths(base_name: str, n_segments: int) -> list[Path]:
    """单段 → base.mp4；多段 → base_001.mp4, base_002.mp4 …"""
    stem = base_name[: -len(".mp4")] if base_name.endswith(".mp4") else base_name
    if n_segments == 1:
        return [Path(f"{stem}.mp4")]
    return [Path(f"{stem}_{i:03d}.mp4") for i in range(1, n_segments + 1)]


def _check_mp4(path: Path) -> bool:
    """mp4 开头应为 ftyp box；顺手挡掉 0 字节 / 明显不是视频的响应。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return len(head) >= 8 and head[4:8] == b"ftyp"
    except OSError:
        return False


def _hint_for_status(status: int) -> str:
    if status in (401, 403):
        return "Token 无效——检查 config.json 的 server.token，或用 --token 传入。"
    if status == 503:
        return (
            "相机未启用感知（native 拉流会话未建立）或 PPCS 未握手——"
            "录制要求该摄像头已在家庭面板启用；本工具加 --ensure-in-use 可自动临时启用。"
            "都不行才考虑 miloco-cli account unbind && account bind 重绑米家账号。"
        )
    if status == 504:
        return (
            "相机在超时内没出一帧——检查：相机在线、与后端同局域网、"
            "镜头未物理遮挡、且该相机已在面板启用感知。"
        )
    return ""


def run(args) -> None:
    url, token = load_server_config(args.url, args.token)

    try:
        cameras = fetch_cameras(url, token)
    except ConnectError as e:
        print(
            f"[错误] 连不上 miloco 后端 {url}（{e}）。"
            f"请确认后端已启动、地址正确（--url 可覆盖）。",
            file=sys.stderr,
        )
        sys.exit(EXIT_NET)
    except ApiError as e:
        hint = _hint_for_status(e.status)
        print(f"[错误] 获取摄像头列表失败（HTTP {e.status}）：{e}", file=sys.stderr)
        if hint:
            print(f"  提示：{hint}", file=sys.stderr)
        sys.exit(EXIT_API)

    if args.list:
        print_camera_list(cameras, url)
        return
    if not cameras:
        print(
            "[错误] 后端已连上，但账号下没有米家摄像头（或未绑定米家账号）。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ARG)

    cam = resolve_camera(cameras, args.camera, args.channel)
    did = cam.get("did")
    channel = args.channel if args.channel is not None else int(cam.get("channel") or 0)
    name = cam.get("name") or "(未命名)"
    room = f"（{cam['room_name']}）" if cam.get("room_name") else ""

    # 单段时长夹到后端合法区间；总时长默认 = 单段（只录一段）
    seg_s = min(max(args.seconds, SEG_MIN_S), SEG_MAX_S)
    if seg_s != args.seconds:
        print(f"[提示] 单段时长夹到 {seg_s:g}s（后端限制 {SEG_MIN_S}-{SEG_MAX_S}s）。")
    total_s = args.total_seconds if args.total_seconds is not None else seg_s

    # 逐段时长：整段取 seg_s；收尾不足 SEG_MIN_S 的尾巴并进末段（不超上限时），
    # 并不进（并后 >SEG_MAX_S）就舍弃并提示。
    durs: list[float] = []
    remaining = total_s
    while remaining >= SEG_MIN_S:
        take = min(seg_s, remaining)
        rest_after = remaining - take
        if 0 < rest_after < SEG_MIN_S and take + rest_after <= SEG_MAX_S:
            take += rest_after
        durs.append(take)
        remaining -= take
    if remaining > 0:
        print(f"[提示] 尾部 {remaining:g}s 不足最短段长 {SEG_MIN_S}s，舍弃。")
    if not durs:
        print(f"[错误] 总时长太短（最短 {SEG_MIN_S}s）。", file=sys.stderr)
        sys.exit(EXIT_ARG)

    ts = datetime.now()
    base_name = args.out or f"miot_recording_{ts:%Y%m%d_%H%M%S}"
    paths = _segment_paths(base_name, len(durs))

    # 预检 + 可选临时启用：record_clip 依赖该摄像头已在活跃集（native PPCS 会话在跑），
    # in_use=false 时后端直接 503。--ensure-in-use 借面板同一开关临时启用，录完恢复。
    was_in_use = bool(cam.get("in_use"))
    toggled = False
    feed_did = f"{did}:ch{channel}" if (cam.get("channel_count") or 1) > 1 else did
    if not was_in_use:
        if args.ensure_in_use:
            print(f"该摄像头未启用感知，--ensure-in-use 临时启用（{feed_did}）…")
            try:
                set_camera_in_use(url, token, feed_did, True)
            except ConnectError as e:
                print(f"[错误] 临时启用时连不上后端：{e}", file=sys.stderr)
                sys.exit(EXIT_NET)
            except ApiError as e:
                print(f"[错误] 临时启用失败（HTTP {e.status}）：{e}", file=sys.stderr)
                hint = _hint_for_status(e.status)
                if hint:
                    print(f"  提示：{hint}", file=sys.stderr)
                sys.exit(EXIT_API)
            toggled = True
            print("已启用（后端正在建立拉流会话，首段开头会多等几秒）。")
        else:
            print(
                "[警告] 该摄像头未启用感知，native 拉流会话未建立，大概率录制失败（HTTP 503）。"
                "在家庭面板启用它，或本工具加 --ensure-in-use 自动临时启用。"
            )

    print(
        f"开始录制：{'单个文件 ' + str(paths[0]) if len(paths) == 1 else f'{len(paths)} 段，形如 ' + str(paths[0])}"
    )
    print(f"  摄像头 did={did}，通道 {channel}，{name}{room}")
    print(
        f"  段长 {'/'.join(f'{d:g}s' for d in durs)}，"
        f"计划总时长约 {total_s:g}s，Ctrl+C 随时停止（当前段作废）。"
    )

    saved: list[tuple[Path, int, float]] = []
    try:
        for i, (path, dur) in enumerate(zip(paths, durs)):
            print(f"[{i + 1}/{len(paths)}] 录制 {dur:g}s …", end="", flush=True)
            t0 = time.perf_counter()
            mp4 = record_clip(url, token, did, channel, dur)
            elapsed = time.perf_counter() - t0
            if not mp4:
                print(f"\n[警告] 第 {i + 1} 段返回空数据，已跳过。", file=sys.stderr)
                continue
            path.write_bytes(mp4)
            print(
                f" 完成，已保存 {path.name}（{len(mp4) / 1e6:.1f} MB，"
                f"后端耗时 {elapsed:.1f}s）"
            )
            if not _check_mp4(path):
                print(f"[警告] {path} 的文件头不像 mp4，请检查内容。", file=sys.stderr)
            saved.append((path, len(mp4), elapsed))
            if i + 1 < len(paths):
                time.sleep(args.pause)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止录制（进行中的段已作废）。")
    except ConnectError as e:
        print(f"\n[错误] 与后端的连接中断：{e}", file=sys.stderr)
        print(f"已完成 {len(saved)} 段，保留在磁盘上。", file=sys.stderr)
        _print_summary(saved)
        sys.exit(EXIT_NET)  # 触发下方 finally 先恢复相机状态
    except ApiError as e:
        print(f"\n[错误] 录制失败（HTTP {e.status}）：{e}", file=sys.stderr)
        hint = _hint_for_status(e.status)
        if hint:
            print(f"  提示：{hint}", file=sys.stderr)
        print(f"已完成 {len(saved)} 段，保留在磁盘上。", file=sys.stderr)
        _print_summary(saved)
        sys.exit(EXIT_API)  # 同上，finally 先恢复相机状态
    finally:
        if toggled:
            _restore_camera_off(url, token, feed_did)

    _print_summary(saved)
    if not saved:
        print("[错误] 一段都没录成，未生成任何文件。", file=sys.stderr)
        sys.exit(EXIT_API)


def _restore_camera_off(url: str, token: str, feed_did: str) -> None:
    """--ensure-in-use 的收尾：把临时启用的摄像头关回原状（尽力而为，失败只警告）。"""
    try:
        set_camera_in_use(url, token, feed_did, False)
        print(f"已恢复 {feed_did} 为未启用状态。")
    except (ApiError, ConnectError) as e:
        print(
            f"[警告] 恢复 {feed_did} 未启用状态失败（{e}），请到家庭面板手动关闭。",
            file=sys.stderr,
        )


def _print_summary(saved: list[tuple[Path, int, float]]) -> None:
    if not saved:
        return
    total_bytes = sum(n for _, n, _ in saved)
    print(f"完成：共 {len(saved)} 段，总大小 {total_bytes / 1e6:.1f} MB。")
    for path, n, elapsed in saved:
        print(f"  {path}（{n / 1e6:.1f} MB，{elapsed:.1f}s）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="用米家摄像头录视频（走 miloco 后端，每段一个 mp4 文件）"
    )
    parser.add_argument("--list", action="store_true", help="列出米家摄像头后退出")
    parser.add_argument(
        "--camera",
        help="摄像头（--list 的序号或 did；多目相机可用合成 did 如 12345:ch1），默认自动挑",
    )
    parser.add_argument("--channel", type=int, help="通道号（多目相机），默认 0 或随合成 did")
    parser.add_argument(
        "--seconds",
        type=float,
        default=SEG_MAX_S,
        help=f"单段时长（秒），默认 {SEG_MAX_S}（后端单段上限 {SEG_MAX_S}s）",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        help="总录制时长（秒）；不设则只录一段。超过单段上限时自动分段落盘",
    )
    parser.add_argument("--out", help="输出路径：单段=文件名，多段=基名（自动加 _NNN.mp4）")
    parser.add_argument(
        "--pause", type=float, default=0.5, help="段间停顿秒数，默认 0.5"
    )
    parser.add_argument(
        "--ensure-in-use",
        action="store_true",
        help="选中摄像头未启用感知时，临时启用它以建立拉流会话，录完自动关回",
    )
    parser.add_argument("--url", help=f"miloco 后端地址，默认 {DEFAULT_URL}（或读配置）")
    parser.add_argument("--token", help="Bearer Token，默认读 $MILOCO_HOME/config.json")
    args = parser.parse_args()

    if args.seconds <= 0 or (args.total_seconds is not None and args.total_seconds <= 0):
        parser.error("--seconds / --total-seconds 须为正数")
    if args.pause < 0:
        parser.error("--pause 不能为负")

    run(args)


if __name__ == "__main__":
    main()
