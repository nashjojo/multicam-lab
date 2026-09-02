#!/usr/bin/env python3
"""多摄像头同步录制：本机摄像头 +（可选）米家摄像头多路，时间对齐。

对齐原理（不靠猜，靠同一个时钟）：
  * 米家侧：后端 ``record_clip`` 响应头 ``X-Clip-First-Frame-Unix-Ms`` 给出该路
    **首帧到达后端时的主机墙钟**。同一 backend 进程内所有相机共用这一个时钟。
  * USB 侧：本进程直接抓帧，首个保留帧的 ``time.time()`` 即其起点，与上面同为
    本机墙钟（同机运行，无跨机时钟偏差）。
  * 于是所有源的起点落在同一时间轴上，清单里给出各源相对最早起点的
    ``start_offset_ms``，据此可精确对齐（见清单里的 ffmpeg 提示）。

  相机侧 PTS 不能用作基准：与主机时钟不同步，且米家 IPC 在「PTS 未知」时会发
  哨兵值 0xFFFFFFFFFFFFFFFF。

  录制**同时发起**（threading.Barrier 栅栏放行），已在感知中启用的相机 PPCS 会话
  是热的，实测三路首帧散布 ~20-60ms；未启用的相机要冷启会话，会拖慢起点，故本
  工具默认对未启用的相机给出警告（``--ensure-in-use`` 可临时启用）。

依赖由本子库的 ``pyproject.toml`` 声明，首次先 ``uv sync`` 建环境，之后：

    # 1) 看有哪些源可用（本机 USB/内置摄像头；miloco 后端可达时还会列出米家各路）
    uv run multi_cam_recorder.py --list

    # 2) 自动挑 USB 摄像头，同步录 15 秒到 sync_clips/ —— 纯 USB 不碰 miloco 后端
    uv run multi_cam_recorder.py --usb-auto --seconds 15

    # 3) 米家是可选源，要一起录得显式指定（需 miloco 后端可达）
    uv run multi_cam_recorder.py --miot-all --usb-auto --seconds 15

    # 4) 指定源（米家用 --list 序号 / did / did:ch1，USB 用索引）
    uv run multi_cam_recorder.py --miot 0 --miot <did>:ch1 \
        --usb 0 --seconds 30

单段时长：纯 USB 不限；带米家源时受后端 ``record_clip`` 限制在 2-60s。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from miot_cam_recorder import (
    DEFAULT_URL,
    SEG_MAX_S,
    SEG_MIN_S,
    ApiError,
    ConnectError,
    _hint_for_status,
    _mark,
    fetch_cameras,
    load_server_config,
    record_clip_with_headers,
    set_camera_in_use,
    stop_clip,
)

EXIT_ARG = 1
EXIT_NET = 2
EXIT_API = 3

# 提前结束：收到 SIGUSR1 就把当前各路收尾（保留已录内容）。
# 用信号而不是 stdin：本工具常被外层脚本包起来、stdout 重定向到日志，
# stdin 不一定可用；信号则无论怎么跑都能送到。
_STOP_REQUESTED = threading.Event()
# 正在录的米家片段 clip_id（供信号处理器转发给后端 stop）。
_ACTIVE_CLIP_IDS: dict[str, tuple[str, str]] = {}   # clip_id -> (url, token)

# USB 预热：栅栏放行前持续抓帧并丢弃，把相机/驱动的缓冲排空，
# 这样放行后的第一个保留帧是真正「当下」的画面，而不是缓冲里的旧帧。
# 同时拿这段测写入帧率（2.5s 而非 1.5s：样本太短时相机吐帧拖动会让
# 估值很噪，实测 1.5s 下同一台相机会在 25-30fps 间跳，直接体现为
# 文件回放速度偏差）。
USB_WARMUP_S = 2.5
FIRST_FRAME_HEADER = "X-Clip-First-Frame-Unix-Ms"


# ─── 录制结果 ────────────────────────────────────────────────────────────────


@dataclass
class Result:
    """单个源的录制结果。``first_unix_ms`` 是对齐用的起点（None = 拿不到）。"""

    kind: str  # "miot" | "usb"
    label: str
    path: Path | None = None
    first_unix_ms: float | None = None
    frames: int | None = None
    size_bytes: int | None = None
    fps: float | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)


# ─── 米家侧 ──────────────────────────────────────────────────────────────────


def resolve_miot_specs(
    cameras: list[dict], specs: list[str], want_all: bool
) -> list[dict]:
    """把 --miot 选择符（序号 / did / did:ch{n}）解析成相机行；--miot-all 取全部。

    裸 did 对多通道相机 = 展开该台所有通道（跟后端 toggle 的裸 did 语义一致）。
    """
    if want_all:
        return list(cameras)

    picked: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def _add(row: dict) -> None:
        key = (str(row.get("did")), int(row.get("channel") or 0))
        if key not in seen:
            seen.add(key)
            picked.append(row)

    for spec in specs:
        matched = False
        # did:ch{n} —— 精确到某路
        if ":ch" in spec:
            base, _, ch_str = spec.rpartition(":ch")
            try:
                ch = int(ch_str)
            except ValueError:
                ch = -1
            for c in cameras:
                if c.get("did") == base and int(c.get("channel") or 0) == ch:
                    _add(c)
                    matched = True
        if not matched:
            # 裸 did —— 多通道展开为全部通道
            hits = [c for c in cameras if c.get("did") == spec]
            if hits:
                for c in hits:
                    _add(c)
                matched = True
        if not matched and spec.isdigit():
            idx = int(spec)
            if 0 <= idx < len(cameras):
                _add(cameras[idx])
                matched = True
        if not matched:
            print(f"[错误] 找不到米家摄像头 {spec!r}，跑 --list 看可用项。", file=sys.stderr)
            sys.exit(EXIT_ARG)
    return picked


def miot_worker(
    url: str,
    token: str,
    cam: dict,
    seconds: float,
    out_path: Path,
    barrier: threading.Barrier,
    result: Result,
) -> None:
    """一路米家摄像头的录制线程：栅栏同步后发起 record_clip，落盘并记起点。"""
    did = str(cam.get("did"))
    channel = int(cam.get("channel") or 0)
    # 客户端生成 clip_id，注册进全局表，以便 SIGUSR1 时能叫后端提前收尾。
    clip_id = uuid.uuid4().hex
    try:
        barrier.wait(timeout=30)
    except threading.BrokenBarrierError:
        result.error = "其他源准备失败，本路取消"
        return
    _ACTIVE_CLIP_IDS[clip_id] = (url, token)
    try:
        mp4, headers = record_clip_with_headers(
            url, token, did, channel, seconds, clip_id=clip_id
        )
    except ApiError as e:
        result.error = f"HTTP {e.status}: {e}"
        return
    except ConnectError as e:
        result.error = f"连接后端失败: {e}"
        return
    finally:
        _ACTIVE_CLIP_IDS.pop(clip_id, None)
    if not mp4:
        result.error = "后端返回空数据"
        return
    out_path.write_bytes(mp4)
    result.path = out_path
    result.size_bytes = len(mp4)
    # 响应头大小写不敏感（http.client 已归一，但保险起见两种都试）
    raw_ts = headers.get(FIRST_FRAME_HEADER) or headers.get(
        FIRST_FRAME_HEADER.lower()
    )
    if raw_ts:
        try:
            result.first_unix_ms = float(raw_ts)
        except ValueError:
            pass
    if result.first_unix_ms is None:
        result.extra["warn"] = (
            "后端未返回首帧时间戳头，无法参与精确对齐"
            "（需要带 X-Clip-First-Frame-Unix-Ms 的后端版本）"
        )


# ─── USB 侧 ──────────────────────────────────────────────────────────────────


def usb_device_names() -> list[str]:
    """本机摄像头名列表（顺序与 OpenCV 索引一致）；不可用时返回空表。"""
    return usb_device_entries()[0]


def usb_device_entries() -> tuple[list[str], list[str | None]]:
    """一次枚举取回 (名字列表, uniqueID 列表)，两者按 OpenCV 索引对齐。

    uniqueID 给 :func:`resolve_cam_mics` 按 USB 身份精确配麦用；取不到时为
    None，配对逻辑自动退回名字匹配。
    """
    try:
        from usb_cam_recorder import camera_entries_for_index
    except Exception:
        return [], []
    try:
        entries, _ = camera_entries_for_index()
    except Exception:
        return [], []
    return [n for n, _ in entries], [u for _, u in entries]


# 连续互通设备（iPhone/iPad 充当摄像头、"桌上视角"）不自动带上：会唤醒手机，
# 且手机在不在身边会改变整张索引表，写死的 --usb N 就指错机位。要用就显式指定。
CONTINUITY_HINTS = ("iphone", "ipad", "desk view", "桌上视角")

# macOS 内建摄像头的常见显示名。不能反过来靠音频端点名称里的 ``audio`` 判断
# 外置复合设备：很多 UVC 摄像头的音频端只叫 ``USB Microphone``，甚至与视频端同名。
BUILTIN_CAMERA_HINTS = ("facetime", "macbook", "built-in", "builtin", "内建")


def _is_builtin_camera(name: str) -> bool:
    """按摄像头端名称识别内建机位；未知设备按外置处理，降级时更安全。"""
    low = name.strip().lower()
    return any(hint in low for hint in BUILTIN_CAMERA_HINTS)


def pick_usb_auto() -> list[int]:
    """自动挑本机摄像头：外置 USB 与内置一并带上，只跳过连续互通设备。"""
    out: list[int] = []
    for i, name in enumerate(usb_device_names()):
        low = name.lower()
        if any(h in low for h in CONTINUITY_HINTS):
            continue
        out.append(i)
    return out


class UsbSetupError(RuntimeError):
    """USB 摄像头准备失败，并携带失败阶段供降级逻辑做因果判断。"""

    def __init__(self, message: str, stage: str):
        super().__init__(message)
        self.stage = stage


class UsbSource:
    """一路 USB 摄像头：预热排空缓冲 → 栅栏放行 → 按墙钟录到时长。"""

    def __init__(self, index: int, width: int, height: int, seconds: float):
        self.index = index
        self.width = width
        self.height = height
        self.seconds = seconds
        self.cap = None
        self.fps = 30.0
        self.actual_w = width
        self.actual_h = height

    def setup(self) -> None:
        """打开设备、下发分辨率、实测帧率。失败抛异常由调用方兜。"""
        import cv2

        from usb_cam_recorder import measure_fps, open_camera

        cap = open_camera(self.index)
        if not cap.isOpened():
            cap.release()
            raise UsbSetupError(
                f"打不开 USB 摄像头 index={self.index}"
                "（macOS 需在 系统设置→隐私与安全性→摄像头 给终端授权，"
                "且设备未被其他应用占用）",
                stage="open",
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        ok, frame = cap.read()
        if not ok:
            cap.release()
            raise UsbSetupError(
                f"USB 摄像头 index={self.index} 能打开但读不到画面"
                "（多半是权限未授予、被占用或 USB 音视频复合设备冲突）",
                stage="first_frame",
            )
        self.actual_h, self.actual_w = frame.shape[:2]
        # VideoWriter 要先知道帧率；很多 UVC 相机上报值不准，实测更可靠。
        self.fps = float(measure_fps(cap))
        self.cap = cap

    def run(
        self, out_path: Path, barrier: threading.Barrier, result: Result
    ) -> None:
        """预热排空 → 栅栏同步 → 抓帧写盘。首个保留帧的墙钟即对齐起点。

        逐帧墙钟同时落一份 sidecar CSV：VideoWriter 只能按**固定**帧率写，
        而 UVC 相机实际吐帧率会漂（光线变暗时尤其明显），两者不等时文件
        时间轴会系统性快/慢放 —— 起点对齐了依然会随时长累积漂移。
        要帧级精确对齐就读这份 CSV（每行 = 该帧的 unix ms）。
        """
        from usb_cam_recorder import open_writer

        assert self.cap is not None

        # 预热：持续取帧**并写入一个丢弃的临时文件**，两个目的。
        # ① 把驱动缓冲排空 —— 否则放行后的第一帧可能是缓冲里几百毫秒前的旧
        #   画面，起点时间戳就与画面内容不符，对齐会系统性偏移。
        # ② 测出**含编码开销的真实循环速率**作为写入帧率。VideoWriter 只能
        #   按固定帧率写，帧率填高了文件就回放偏快。实测教训：只用 grab 测
        #   得 29-31fps、改用 read 仍测得 30fps，而真正录制时只有 26-27fps ——
        #   差就差在 ``writer.write()`` 的 H.264 编码耗时上（~11%），不把写盘
        #   算进去就永远高估。故预热阶段原样跑一遍写盘再把临时文件删掉。
        tmp_path = out_path.with_name(out_path.stem + ".warmup.mp4")
        warm_writer, _ = open_writer(
            str(tmp_path), self.actual_w, self.actual_h, round(self.fps)
        )
        warm_frames = 0
        warm_t0 = time.perf_counter()
        warm_until = warm_t0 + USB_WARMUP_S
        while time.perf_counter() < warm_until:
            ok, frame = self.cap.read()
            if ok:
                warm_frames += 1
                if warm_writer is not None:
                    warm_writer.write(frame)
        warm_elapsed = time.perf_counter() - warm_t0
        if warm_writer is not None:
            warm_writer.release()
        try:
            tmp_path.unlink()
        except OSError:
            pass
        if warm_frames >= 10 and warm_elapsed > 0:
            warm_fps = max(1.0, min(warm_frames / warm_elapsed, 120.0))
            # ⬇ 合理性兜底：预热只有 2.5s，若此时有其它子系统正在初始化而抖到
            # CPU/USB 带宽，测值会荒谬地低（实测踩过：同时起音频采集时测得 4fps，
            # 而真实录制达 24.8fps，文件就被写成 4fps、回放慢 6 倍）。偏离 setup()
            # 那次估值超过一半就不信预热值。
            if warm_fps < 0.5 * self.fps:
                print(f"[提示] usb[{self.index}] 预热测得 {warm_fps:.1f}fps 远低于"
                      f"初估 {self.fps:.1f}fps（资源竞争？），改用初估值。", flush=True)
            else:
                self.fps = warm_fps

        # ⬇ 写入帧率必须先取整并**存下来**：VideoWriter 只接整数帧率，而下面算
        # playback_speed_ratio 时必须用它、不能用未取整的实测值 —— 两者不一致会
        # 让回放时间轴死偏（实测踩过：26.40 取整成 26 但 ratio 用 26.40 算，
        # 32s 处就偏 0.5s，且偏的方向随“四舍/五入”翻转）。
        written_fps = max(1, int(round(self.fps)))
        writer, fourcc = open_writer(
            str(out_path), self.actual_w, self.actual_h, written_fps
        )
        if writer is None:
            result.error = f"无法创建视频文件 {out_path}"
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass
            return

        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            result.error = "其他源准备失败，本路取消"
            writer.release()
            return

        frames = 0
        fail_streak = 0
        first_ts: float | None = None
        stamps: list[float] = []
        t_start = time.perf_counter()
        while True:
            ok, frame = self.cap.read()
            now = time.time()
            if not ok:
                fail_streak += 1
                if fail_streak > 60:  # 偶发丢帧正常，连续失败才算断流
                    result.extra["warn"] = "连续读取失败，提前结束"
                    break
                continue
            fail_streak = 0
            if first_ts is None:
                first_ts = now
            stamps.append(now)
            writer.write(frame)
            frames += 1
            if _STOP_REQUESTED.is_set():
                result.extra["stopped_early"] = True
                break
            if time.perf_counter() - t_start >= self.seconds:
                break

        writer.release()
        elapsed = time.perf_counter() - t_start
        result.path = out_path
        result.frames = frames
        result.first_unix_ms = first_ts * 1000 if first_ts else None
        # 对下游而言，“文件的名义帧率”才是有用的（拿它算帧号、算时间轴），
        # 所以这里报写入值；未取整的预热实测值另存作诊断用。
        result.fps = float(written_fps)
        result.extra["fps_written"] = written_fps
        result.extra["fps_prewarm_measured"] = round(self.fps, 2)
        result.extra["fourcc"] = fourcc
        result.extra["size"] = f"{self.actual_w}x{self.actual_h}"
        try:
            result.size_bytes = out_path.stat().st_size
        except OSError:
            pass

        # 写入帧率（固定）vs 实测帧率（真实采集）的比值 = 文件回放速度偏差。
        # >1 表示文件放得比现实快，时间轴逐渐超前。下游可用 ffmpeg 的
        # ``setpts=PTS*ratio`` 校回，或直接用 sidecar 的逐帧时间戳。
        # 分子必须是 written_fps（文件真正用的帧率），用未取整的 self.fps 会造成
        # 随时长累积的时间轴偏移（实测：32s 处 0.5s）。
        # 用帧 stamps 算时长：perf_counter 包含首帧 cap.read() 的等待开销，
        # 会比实际帧跨度多 1-2s，导致 viewer timeline 超出帧数据范围。
        # 算帧率要拿**间隔数**去除时长：N 个时间戳之间只有 N-1 个帧间隔，用 N 会
        # 系统性偏高（30 帧时高 3.4%，正好顶穿下面 3% 的告警门槛；900 帧才 0.1%）。
        if len(stamps) >= 2:
            frame_span = stamps[-1] - stamps[0]
            intervals = frames - 1
        else:
            frame_span = elapsed
            intervals = frames
        if intervals and frame_span > 0:
            measured = intervals / frame_span
            result.extra["fps_actual"] = round(measured, 2)
            result.extra["duration_wall_s"] = round(frame_span, 3)
            ratio = written_fps / measured if measured else 1.0
            result.extra["playback_speed_ratio"] = round(ratio, 4)
            # 门槛取 3%：10s 素材上 3% 已是 0.3s 漂移，对多机对齐已经不容忽略。
            if abs(ratio - 1.0) > 0.03:
                result.extra["warn"] = (
                    f"文件按 {written_fps}fps 写入但实际采集 {measured:.1f}fps，"
                    f"回放速度偏差 {(ratio - 1) * 100:+.1f}%（{frame_span:.1f}s 录成 "
                    f"{frames / written_fps:.1f}s）；帧级对齐请用 sidecar 时间戳"
                )

        # 逐帧墙钟 sidecar（每行一个 unix ms，与文件帧序一一对应）
        if stamps:
            ts_path = out_path.with_name(out_path.stem + "_frames.csv")
            ts_path.write_text(
                "frame_index,unix_ms\n"
                + "".join(f"{i},{t * 1000:.1f}\n" for i, t in enumerate(stamps)),
                encoding="utf-8",
            )
            result.extra["frame_timestamps"] = ts_path.name

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


# ─── 音频采集（--audio，可选） ─────────────────────────────────────────

# 音频 WS 的可选时间戳头（后端 MIoTAudioStreamManager，靠 ?ts=1 开启）：
#   uint8 版本 | 3B 填充 | uint32 seq | uint64 相机侧 ts(ms) | uint64 主机墙钟(unix ms)
AUDIO_HDR_FMT = ">B3xIQQ"
AUDIO_HDR_BYTES = 24


@dataclass
class AudioTrack:
    """一路音频的采集结果。"""

    label: str
    kind: str                      # "miot" | "usb"
    device_name: str | None = None  # 实际使用的 PortAudio 设备名
    # 麦克风是怎么配上的："uid"（USB 身份精确匹配）/ "name"（名字）/
    # "default"（内建机位兜到系统默认输入）。写进清单：同型号多机位下只有
    # "uid" 才能保证没配串，事后能据此判断这路收音到底可不可信。
    match_by: str | None = None
    wav: Path | None = None
    csv: Path | None = None
    sample_rate: int = 0
    samples: int = 0
    packets: int = 0
    first_unix_ms: float | None = None
    seq_gaps: int = 0
    error: str | None = None


def _write_wav(path: Path, pcm_i16: bytes, sample_rate: int) -> None:
    """写单声道 16-bit PCM WAV（标准库 wave，不额外引依赖）。"""
    import wave

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_i16)


def _write_audio_sidecar(path: Path, rows: list) -> None:
    """逐包时间戳：``sample_index,unix_ms,n_samples``。

    为何需要：WAV 自身只有“第几个采样”而无绝对时间，且网络丢包会让
    “采样号 ÷ 采样率”与真实时间逐渐脱钩。有了逐包主机墙钟，下游能把任意
    采样位置映回绝对时刻（与 USB 逐帧时间戳 sidecar 同一思路）。
    """
    path.write_text(
        "sample_index,unix_ms,n_samples\n"
        + "".join(f"{i},{t:.1f},{n}\n" for i, t, n in rows),
        encoding="utf-8",
    )


async def _collect_miot_audio(
    ws_url: str, out_dir: Path, tag: str,
    stop: threading.Event, track: AudioTrack,
) -> None:
    """一路米家音频：WS(opus) → PCM → WAV + 逐包时间戳。

    时间戳取头里的 ``recv_unix_ms``（后端回调入口打的点）而不是客户端收到的时刻，
    可少一道 WS 转发延迟（实测 loopback 上约 1.1–1.6ms）。
    """
    import av
    import websockets

    dec = av.CodecContext.create("opus", "r")
    dec.sample_rate = 48000
    dec.layout = "mono"
    pcm_parts: list = []
    rows: list = []
    cum = 0
    last_seq = None
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
                if isinstance(msg, str) or len(msg) <= AUDIO_HDR_BYTES:
                    continue
                _ver, seq, _cam_ts, recv_ms = struct.unpack(
                    AUDIO_HDR_FMT, msg[:AUDIO_HDR_BYTES]
                )
                if last_seq is not None and seq != last_seq + 1:
                    track.seq_gaps += 1
                last_seq = seq
                for fr in dec.decode(av.Packet(msg[AUDIO_HDR_BYTES:])):
                    arr = fr.to_ndarray()
                    if arr.dtype.kind == "f":            # fltp → int16
                        arr = (arr.clip(-1, 1) * 32767).astype("int16")
                    arr = arr.astype("int16", copy=False).ravel()
                    pcm_parts.append(arr.tobytes())
                    rows.append((cum, float(recv_ms), int(arr.size)))
                    if track.first_unix_ms is None:
                        track.first_unix_ms = float(recv_ms)
                    cum += int(arr.size)
                track.packets += 1
    except Exception as e:
        track.error = f"{type(e).__name__}: {e}"
        return
    if not pcm_parts:
        track.error = "无音频数据（拾音未开 / 该通道无麦克风）"
        return
    track.wav = out_dir / f"{tag}_audio.wav"
    track.csv = out_dir / f"{tag}_audio.csv"
    track.sample_rate = 48000
    track.samples = cum
    _write_wav(track.wav, b"".join(pcm_parts), 48000)
    _write_audio_sidecar(track.csv, rows)


def _collect_mic_audio(
    dev: int, out_dir: Path, tag: str,
    stop: threading.Event, track: AudioTrack,
    gate: threading.Event | None = None, preroll_s: float = 0.5,
    startup_done: threading.Event | None = None,
) -> None:
    """本机麦克风（USB 摄像头自带）→ WAV + 逐块时间戳。

    流在视频前就得开着（见 :func:`_start_usb_mic_capture`），但数据只在 ``gate``
    放行（栅栏释放）后才入文件；此前只滚动保留末段 ``preroll_s`` 秒，
    放行时先冲进队列，保住视频起点对应的那点声音。``gate`` 为 None 则全程收录。
    """
    import queue as _q
    from collections import deque

    import numpy as np
    import sounddevice as sd

    try:
        sr = int(sd.query_devices(dev)["default_samplerate"])
    except Exception as e:
        track.error = f"{type(e).__name__}: {e}"
        if startup_done is not None:
            startup_done.set()
        return
    q = _q.Queue()
    # blocksize=1024，预卷容量按块数折算；至少留 1 块。
    pre: deque = deque(maxlen=max(1, int(preroll_s * sr / 1024)))
    flushed = False

    def cb(indata, frames, time_info, status):
        nonlocal flushed
        blk = (time.time(), indata[:, 0].copy())
        if gate is None or gate.is_set():
            if not flushed:
                for p in pre:
                    q.put(p)
                pre.clear()
                flushed = True
            q.put(blk)
        else:
            pre.append(blk)

    try:
        with sd.InputStream(device=dev, channels=1, samplerate=sr,
                            dtype="float32", callback=cb, blocksize=1024):
            # 只有 InputStream 已成功进入 active 状态才放行调用方去开视频。
            # t.start() 本身不提供这个保证；缺少握手会让音视频开流顺序产生竞态。
            if startup_done is not None:
                startup_done.set()
            while not stop.is_set():
                time.sleep(0.1)
    except Exception as e:
        track.error = f"{type(e).__name__}: {e}"
        return
    finally:
        # 失败也必须唤醒等待者；调用方通过 track.error 区分成功与失败。
        if startup_done is not None:
            startup_done.set()
    parts: list = []
    rows: list = []
    cum = 0
    while not q.empty():
        ts, blk = q.get()
        i16 = (np.clip(blk, -1, 1) * 32767).astype("int16")
        parts.append(i16.tobytes())
        # 回调时刻对应该块**末尾**，回退一块时长得到块起点
        start_ms = (ts - len(blk) / sr) * 1000
        rows.append((cum, start_ms, int(i16.size)))
        if track.first_unix_ms is None:
            track.first_unix_ms = start_ms
        cum += int(i16.size)
        track.packets += 1
    if not parts:
        track.error = "麦克风无数据"
        return
    track.wav = out_dir / f"{tag}_audio.wav"
    track.csv = out_dir / f"{tag}_audio.csv"
    track.sample_rate = sr
    track.samples = cum
    _write_wav(track.wav, b"".join(parts), sr)
    _write_audio_sidecar(track.csv, rows)


# 设备名里只表示「这是镜头还是麦」的后缀词，比较前得先去掉。
# 顺序有意义："-audio" 要先于 "audio"、"microphone" 要先于其他。
_MIC_NAME_NOISE = ("-audio", "audio", "camera", "microphone",
                   "相机", "摄像头", "麦克风")


def _name_stem(name: str) -> str:
    """把设备名归一成可比对的词干（去掉镜头/麦克风类后缀与多余空白）。

    同一硬件的视频端与音频端往往只差一个后缀：
    "1080P USB Camera" ↔ "1080P USB Camera-Audio"、
    "MacBook Pro相机" ↔ "MacBook Pro麦克风"。只做子串匹配会漏掉后者
    （「相机」与「麦克风」没有公共子串），于是本可用的内建麦被白白丢掉。
    """
    s = name.strip().lower()
    for w in _MIC_NAME_NOISE:
        s = s.replace(w, " ")
    return " ".join(s.split())


def resolve_cam_mics(usb_indices: list, cam_names: list,
                     cam_uids: list | None = None) -> dict:
    """给每路 USB 摄像头配自带麦克风，**一个输入设备只能分给一路**。

    返回 ``{摄像头索引: (PortAudio 设备号, 设备名, 匹配依据) | None}``，匹配
    依据取 ``"uid"`` / ``"name"`` / ``"default"``。

    匹配策略按可靠程度从高到低：

    1. **按 USB 身份精确匹配**（有 ``cam_uids`` 时）：视频端 uniqueID → USB
       location → 序列号 → 含该序列号的音频设备，见 :mod:`mac_device_ids`。
       只有这一步能分开**同型号的两台摄像头** —— 它们的视频端与音频端名字
       完全一样，名字匹配可能两路交叉，而两路都有数据、标签也对，属于
       静默错配。它同时能避开单台设备的同名歧义（实测 1080P USB Camera
       的麦与扬声器在 PortAudio 里同名）。
    2. 按名字匹配：同一硬件的视频端与音频端往往只差一个后缀
       （"1080P USB Camera" ↔ "1080P USB Camera-Audio"）。
    3. 仅对**内建机位**回退到系统默认输入设备：内建镜头与内建麦有时压根不同名
       （"FaceTime HD Camera" ↔ "MacBook Pro Microphone"），名字匹配会把本可用的
       内建麦白白丢掉；而内建麦与内建镜头同属一台机器，归到该机位名下不算错标。

    **外置相机配不上就明确留空，不做任何借用**：那会让清单里某个外置机位标着自己的
    收音、实际录的却是别处的麦 —— 比没有音频更糟，事后无从分辨。实测过按名字借麦的
    后果：三路 wav 的 RMS(1564/1570/1564)、峰值(13840)与采样率(均 16kHz)完全一致，
    全部来自同一个 1080P USB Camera-Audio。

    两种匹配都**不看 PortAudio 设备索引**：索引会因设备插拔、甚至同一台相机的
    视频被占用而变动（实测同一索引下一轮就变成其它设备，报 Invalid number of
    channels）。
    """
    try:
        import sounddevice as sd
    except Exception:
        return {i: None for i in usb_indices}
    devs = sd.query_devices()
    ins = [(i, d["name"]) for i, d in enumerate(devs)
           if d["max_input_channels"] > 0]
    valid_in = {i for i, _ in ins}
    taken: set = set()
    out: dict = {i: None for i in usb_indices}
    # 第一轮：按 USB 身份精确匹配（唯一能分开同型号两台的办法）
    if cam_uids:
        uid_of = {cam: (cam_uids[cam] if cam < len(cam_uids) else None)
                  for cam in usb_indices}
        try:
            from mac_device_ids import mic_by_camera_uid
            # 只交本次在录的机位：序列号撞车的判定要限在这些机位之间，
            # 没在录的同型号设备不应该影响本次配对。
            hits = mic_by_camera_uid([u for u in uid_of.values() if u])
        except Exception:
            hits = {}
        for cam in usb_indices:
            uid = uid_of[cam]
            hit = hits.get(uid) if uid else None
            # 再校一道 PortAudio 侧确实能输入：两套枚举之间设备可能刚好变动
            if hit and hit[0] in valid_in and hit[0] not in taken:
                out[cam] = (hit[0], hit[1], "uid")
                taken.add(hit[0])
    # 第二轮：名字匹配
    for cam in usb_indices:
        if out[cam] is not None:
            continue
        stem = _name_stem(cam_names[cam] if cam < len(cam_names) else "")
        if not stem:
            continue
        for dev_i, dev_name in ins:
            if dev_i not in taken and _name_stem(dev_name) == stem:
                out[cam] = (dev_i, dev_name, "name")
                taken.add(dev_i)
                break
    # 第三轮：只给**内建机位**兜底到系统默认输入设备（外置机位一律留空，见上）。
    builtin_unmatched = [
        cam for cam, dev in out.items()
        if dev is None and _is_builtin_camera(
            cam_names[cam] if cam < len(cam_names) else "")
    ]
    if builtin_unmatched:
        # 读 sd.default.device 恒返回 [输入, 输出] 二元组；无可用输入时该位为 -1，
        # 与任何真实设备号都对不上，自然跳过。
        default_dev = sd.default.device[0]
        for dev_i, dev_name in ins:
            if dev_i == default_dev and dev_i not in taken:
                # 默认输入设备只有一个，最多兜给一路，仍不破坏「一麦一路」。
                out[builtin_unmatched[0]] = (dev_i, dev_name, "default")
                taken.add(dev_i)
                break
    return out


@dataclass
class EarlyAudio:
    """视频开流前已启动的 USB 麦克风，以及一次解析得到的稳定设备映射。"""

    stop: threading.Event
    threads: list[threading.Thread]
    tracks: list[AudioTrack]
    mic_of: dict[int, tuple[int, str, str] | None]
    active_cams: set[int]


def _start_usb_mic_capture(
    usb_indices: list, out_dir: Path, gate: threading.Event | None = None,
) -> EarlyAudio:
    """起 USB 自带麦克风并等到开流成功/失败后返回。

    **必须在摄像头视频流活跃之前调用**：USB 摄像头是复合设备（视频与麦克风同属一个
    USB 设备），视频在采集时开同一设备的音频输入流会被 macOS 拒绝，PortAudio 报
    ``Internal PortAudio error [PaErrorCode -9986]``、CoreAudio 侧为 ``AUHAL err='35'``。

    但反过来也有坑（见 :func:`_discard_early_audio` 处的降级逻辑）：麦流持续活跃时，
    同一 USB 集线器下的第二支摄像头可能完全不出帧（实测与分辨率/压缩格式无关）。
    故调用方需在摄像头准备失败时停麦降级。
    对齐不靠「同时开始」而靠每个音频包的主机时间戳，提前开流没有副作用。
    """
    stop = threading.Event()
    threads: list = []
    tracks: list = []
    names, uids = usb_device_entries()
    mics = resolve_cam_mics(usb_indices, names, uids)
    startups: list[tuple[int, AudioTrack, threading.Event]] = []
    for idx in usb_indices:
        tr = AudioTrack(label=f"usb[{idx}] 麦克风", kind="usb")
        tracks.append(tr)
        mic_info = mics.get(idx)
        if mic_info is None:
            tr.error = ("无可用麦克风（USB 身份与名字都没配上对应的输入设备；"
                        "仅内建机位回退到系统默认输入）")
            continue
        dev, dev_name, match_by = mic_info
        tr.device_name = dev_name
        tr.match_by = match_by
        startup_done = threading.Event()
        t = threading.Thread(target=_collect_mic_audio,
                             args=(dev, out_dir, f"usb{idx}", stop, tr),
                             kwargs={"gate": gate, "startup_done": startup_done},
                             daemon=True)
        try:
            t.start()
        except Exception as e:
            tr.error = f"{type(e).__name__}: {e}"
            continue
        threads.append(t)
        startups.append((idx, tr, startup_done))

    # 等所有 InputStream 明确 active 或失败，严格保证调用方随后打开视频时音频端
    # 已经完成开流；不能只等线程启动。用总 deadline，避免多路设备串行放大超时。
    deadline = time.monotonic() + 10.0
    timed_out = False
    for _, tr, done in startups:
        if not done.wait(timeout=max(0.0, deadline - time.monotonic())):
            tr.error = "麦克风开流超时（10s）"
            timed_out = True
    if timed_out:
        # 不允许尚未确认状态的 PortAudio 线程在视频打开后才迟到开流。
        stop.set()
        for t in threads:
            t.join(timeout=5)
        if any(t.is_alive() for t in threads):
            raise RuntimeError("USB 麦克风开流线程无法停止；为避免音视频设备竞态，取消录制")
        return EarlyAudio(threading.Event(), [], tracks, mics, set())

    active_cams = {
        idx for idx, tr, _ in startups
        if tr.error is None
    }
    return EarlyAudio(stop, threads, tracks, mics, active_cams)


def _discard_early_audio(early: EarlyAudio, reason: str | None = None) -> list:
    """停掉提前起的 USB 麦采集并清掉已落盘的文件（降级为无麦时用）。

    采集线程在 stop 后会排干队列并写 WAV/CSV，故必须先 join 再删文件。

    返回被停掉的音轨（``reason`` 非空时会写进本来正常那几路的 ``error``）。
    **调用方要把它们并进清单**：否则被降级停掉的机位在清单里凭空消失，
    事后根本看不出这一路为何没有音频（实测踩过：三路只剩一路音频，
    manifest 里毫无线索）。
    """
    # 先记下本来正常的几路（停流后它们的 error 会被采集线程抹成无数据）
    was_running = [tr for tr in early.tracks if tr.error is None]
    early.stop.set()
    for t in early.threads:
        t.join(timeout=5)
    for tr in early.tracks:
        for p in (tr.wav, tr.csv):
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass
        # 文件已删，清掉引用与计数，免得清单指向不存在的 wav
        tr.wav = tr.csv = None
        tr.sample_rate = tr.samples = tr.packets = 0
    # 本来正常那几路：停流后采集线程会因为一帧未收而把 error 写成
    # 「麦克风无数据」（降级发生在放行前，门控未开、本就没数据）——
    # 那是本次停麦造成的假象，得用真正的降级原因盖掉；而停麦前就已有的
    # 错误（如“无可用麦克风”）是真实原因，不能动。
    if reason:
        for tr in was_running:
            tr.error = reason
    return list(early.tracks)


# 降级停麦时给每路留的解释，跟清单里的 audio_downgrade 互相对应。
_MIC_DROPPED_FMT = ("已被音频降级停用（触发：{trigger} 读不到首帧），"
                    "原因详见清单 audio_downgrade")
_MIC_ROLLBACK_FMT = ("降级回滚时停用：重起摄像头麦后 {broken} 反而准备失败，"
                     "为保住视频放弃本路录音")


def _resolve_mic_video_conflict(
    usb_sources: list, conflict_failed: list, early_audio: EarlyAudio,
    external_mic_cams: set, out_dir: Path, gate: threading.Event,
) -> tuple:
    """处理「摄像头读不到首帧」与「外置复合麦活跃」同时出现的冲突。

    返回 ``(新的 early_audio, 未能起回的音轨, 降级记录 dict)``。

    先停掉摄像头麦重试失败的机位，**再按重试结果反推麦到底是不是元凶**：

    * 有机位因此恢复 → 坐实是 USB 麦流与同一集线器下多路视频冲突，外置麦
      保持停用（只起回内建麦）。
    * 一路都没恢复 → 麦是无辜的（多为摄像头卡死或 USB 带宽/供电问题），
      把其余机位的麦**重新起回来**。早期版本在这里一停了之，会为了一路
      跟麦无关的摄像头故障，白白牺牲另一路本来正常的录音。

    重起外置复合麦必须先放掉该机位的视频：复合设备上视频在采集时开音频输入
    会被 macOS 驳回（PortAudio -9986）。若重起后反而把本来正常的机位弄挂，
    则回滚成不带摄像头麦的状态 —— 视频是主产物，不能为录音冒险。
    """
    trigger = ", ".join(r.label for _, r in conflict_failed)
    stopped = [tr.label for tr in early_audio.tracks if tr.error is None]
    print(f"[警告] {trigger} 在摄像头麦开流时读不到首帧，先停掉摄像头麦重试…",
          file=sys.stderr)
    dropped = _discard_early_audio(
        early_audio, _MIC_DROPPED_FMT.format(trigger=trigger))

    recovered: list = []
    still_failed: list = []
    for s, r in conflict_failed:
        if s.cap is not None:
            s.cap.release()
            s.cap = None
        try:
            s.setup()
        except Exception as e:
            r.error = str(e)
            still_failed.append(r.label)
            print(f"[警告] {r.label} 停麦后仍准备失败：{e}", file=sys.stderr)
            continue
        r.error = None
        r.extra.pop("setup_failure_stage", None)
        r.extra["size"] = f"{s.actual_w}x{s.actual_h}"
        recovered.append(r.label)

    alive = [s.index for s, r in usb_sources if r.error is None]
    if recovered:
        verdict = "confirmed"
        note = (f"停掉摄像头麦后 {', '.join(recovered)} 恢复出帧，确认是 USB 麦流与"
                "同一集线器下多路视频冲突；本次外置摄像头麦全程停用，内建麦保留。")
        keep = [i for i in alive if i not in external_mic_cams]
    else:
        verdict = "ruled_out"
        note = (f"停掉摄像头麦后 {trigger} 仍读不到首帧，说明麦流不是原因"
                "（多为摄像头卡死或 USB 带宽/供电问题，可重插该摄像头再试）；"
                "其余机位的麦已重新起回。")
        keep = alive

    new_early = None
    restarted: list = []
    if keep:
        if verdict == "ruled_out":
            # 复合麦要在视频之前开，先把这些机位的视频放掉
            for s, r in usb_sources:
                if s.index in keep and s.cap is not None:
                    s.cap.release()
                    s.cap = None
        try:
            new_early = _start_usb_mic_capture(keep, out_dir, gate)
            restarted = [tr.label for tr in new_early.tracks if tr.error is None]
        except Exception as e:
            print(f"[警告] 降级后重起麦克风失败：{e}", file=sys.stderr)
        if verdict == "ruled_out":
            for s, r in usb_sources:
                if s.index in keep:
                    _resetup(s, r)
            broken = [r.label for s, r in usb_sources
                      if s.index in keep and r.error is not None]
            if broken and new_early is not None:
                print(f"[警告] 重起摄像头麦后 {', '.join(broken)} 反而准备失败，"
                      "回滚为不带摄像头麦…", file=sys.stderr)
                dropped += _discard_early_audio(
                    new_early, _MIC_ROLLBACK_FMT.format(broken=", ".join(broken)))
                new_early = None
                restarted = []
                verdict = "ruled_out_rolled_back"
                note += f" 但重起麦后 {', '.join(broken)} 准备失败，已回滚为仅录视频。"
                for s, r in usb_sources:
                    if s.index in keep and r.error is not None:
                        _resetup(s, r)

    info = {
        "trigger": [r.label for _, r in conflict_failed],
        "trigger_stage": "first_frame",
        "mics_stopped": stopped,
        "cams_recovered": recovered,
        "cams_still_failed": still_failed,
        "mics_restarted": restarted,
        "verdict": verdict,
        "note": note,
    }
    return new_early, dropped, info


def _resetup(src, res) -> None:
    """重新准备一路 USB 摄像头，成败写回 ``res``（降级流程专用）。"""
    if src.cap is not None:
        src.cap.release()
        src.cap = None
    try:
        src.setup()
    except Exception as e:
        res.error = str(e)
        return
    res.error = None
    res.extra.pop("setup_failure_stage", None)
    res.extra["size"] = f"{src.actual_w}x{src.actual_h}"


def start_audio_capture(
    url: str, token: str, picked: list, out_dir: Path,
    early: EarlyAudio | None,
) -> tuple:
    """起剩余音频采集线程（米家音频流），与早起的 USB 麦汇合后一起返回。
    签名与返回值：``(停止信号, 线程列表, 全部结果容器)``。
    ``early`` 是 :func:`_start_usb_mic_capture` 的返回值（可为 ``None``）。

    **米家音频不参与视频的栅栏同起**：每个音频包自带主机时间戳，对齐靠时间戳而不靠
    “同时开始”；提前开、滑后关反而能把视频窗口完整覆盖进去。
    """
    if early is not None:
        stop, threads, tracks = early.stop, early.threads, early.tracks
    else:
        stop = threading.Event()
        threads = []
        tracks = []
    ws_base = url.replace("http://", "ws://").replace("https://", "wss://")

    jobs = []
    for c in picked:
        cc = c.get("channel_count") or 1
        ch = int(c.get("channel") or 0)
        tag = f"miot_{c['did']}_ch{ch}" if cc > 1 else f"miot_{c['did']}"
        tr = AudioTrack(label=f"米家 {c['did']}:ch{ch}", kind="miot")
        tracks.append(tr)
        jobs.append((
            f"{ws_base}/api/miot/ws/audio_stream"
            f"?camera_id={c['did']}&channel={ch}&ts=1&token={token}",
            tag, tr,
        ))

    if jobs:
        def _run_loop() -> None:
            async def _all() -> None:
                await asyncio.gather(*[
                    _collect_miot_audio(u, out_dir, tg, stop, tr)
                    for u, tg, tr in jobs
                ])
            asyncio.run(_all())

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()
        threads.append(t)

    return stop, threads, tracks


# ─── 列表 / 清单 ─────────────────────────────────────────────────────────────


def print_sources(cameras: list[dict], url: str) -> None:
    if cameras:
        print(f"米家摄像头（GET {url}/api/miot/scope/cameras）：")
        for i, c in enumerate(cameras):
            name = c.get("name") or "(未命名)"
            room = f"（{c['room_name']}）" if c.get("room_name") else ""
            cc = c.get("channel_count") or 1
            sel = f"{c.get('did')}:ch{c.get('channel')}" if cc > 1 else str(c.get("did"))
            print(
                f"  [{i}] {name}{room}  通道 {c.get('channel')}/{cc - 1}  "
                f"云端{_mark(c.get('cloud_online'))} "
                f"局域网{_mark(c.get('lan_reachable'))} "
                f"镜头{_mark(c.get('awake'))} "
                f"感知启用{'✓' if c.get('in_use') else '✗'}   --miot {sel}"
            )
    names = usb_device_names()
    auto = set(pick_usb_auto())
    print("\n本机摄像头（索引与 OpenCV 一致）：")
    if names:
        for i, n in enumerate(names):
            hint = "  ← 默认自动录" if i in auto else "  ← 连续互通，要录得显式指定"
            print(f"  [{i}] {n}{hint}   --usb {i}")
    else:
        print("  （枚举不到；装 opencv-python + pyobjc-framework-AVFoundation 后重试）")
    if cameras:
        print("\n提示：录制要求米家摄像头「感知启用✓」，否则拉流会话是冷的，"
              "起点会明显滞后（可加 --ensure-in-use 临时启用）。")


def build_manifest(results: list[Result], seconds: float,
                   audio: list | None = None,
                   audio_downgrade: dict | None = None) -> dict:
    """汇总清单：以最早起点为基准给出各源偏移，并附对齐用的 ffmpeg 提示。"""
    ok = [r for r in results if r.error is None and r.first_unix_ms is not None]
    ref = min((r.first_unix_ms for r in ok), default=None)

    sources = []
    for r in results:
        item: dict = {
            "kind": r.kind,
            "label": r.label,
            # 只存基名：清单与素材同居一个 clip 文件夹，消费方按「清单所在目录」
            # 解析即可，整个文件夹搬到哪里都不断链。
            "file": r.path.name if r.path else None,
            "first_frame_unix_ms": r.first_unix_ms,
            "start_offset_ms": (
                round(r.first_unix_ms - ref, 1)
                if (ref is not None and r.first_unix_ms is not None)
                else None
            ),
            "frames": r.frames,
            "size_bytes": r.size_bytes,
            "fps": r.fps,
            "error": r.error,
        }
        item.update({k: v for k, v in r.extra.items()})
        sources.append(item)

    offsets = [s["start_offset_ms"] for s in sources if s["start_offset_ms"] is not None]
    manifest = {
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_s": seconds,
        "time_base": "unix epoch milliseconds, host wall clock",
        "reference_unix_ms": ref,
        "sources": sources,
        "alignment": {
            "max_offset_ms": round(max(offsets), 1) if offsets else None,
            "how_to": (
                "各源已同时发起录制；start_offset_ms 是该源相对最早源的起点滞后。"
                "要硬对齐，用 ffmpeg 把每个文件按自身 offset 裁掉开头："
                "ffmpeg -ss <offset_ms/1000> -i <file> -c copy <aligned>"
            ),
            "caveat": (
                "米家侧起点取「首帧到达后端」的时刻，含相机→主机的传输与解码延迟"
                "（同型号相机之间基本同量，不同型号可能有数十毫秒系统性差异）；"
                "USB 侧起点是本机抓帧时刻。两者都在同一主机时钟上，无跨机偏差。"
            ),
            "timebase_caveat": (
                "注意回放时间轴：两侧文件都按**固定名义帧率**写入（米家侧后端固定 "
                "30fps，USB 侧取录前实测值），而真实吐帧率会漂。若 sources 里的 "
                "playback_speed_ratio 偏离 1，说明该文件回放快/慢，起点对齐后仍会随"
                "时长累积漂移。需帧级精确对齐时，用 USB 源的 frame_timestamps "
                "sidecar（逐帧 unix ms）做时间映射，而不是依赖文件帧率。"
            ),
        },
    }
    if audio:
        # 音频单列一组而不往 sources 里塞：一路视频未必有音（双摄 ch1 无麦克风），
        # 且音频不参与栅栏同起、时间轴自带，与视频源是正交的两组东西。
        manifest["audio"] = [
            {
                "label": a.label,
                "kind": a.kind,
                "device_name": a.device_name,
                "match_by": a.match_by,
                "file": a.wav.name if a.wav else None,
                "timestamps": a.csv.name if a.csv else None,
                "sample_rate": a.sample_rate or None,
                "samples": a.samples or None,
                "packets": a.packets or None,
                "first_unix_ms": a.first_unix_ms,
                "seq_gaps": a.seq_gaps,
                "error": a.error,
            }
            for a in audio
        ]
        manifest["audio_note"] = (
            "WAV 只有采样序、无绝对时间；要对到视频时间轴请用 timestamps "
            "sidecar（sample_index,unix_ms,n_samples）。米家侧时间戳取自音频 WS 的 "
            "?ts=1 头里的 recv_unix_ms（帧到达后端的主机墙钟）。注意：实测相机侧 "
            "音频 ts 恒为 0（未填或哨兵值），所以**无法**用相机 PTS 做音视频桥接，"
            "只能靠主机墙钟对齐。"
        )
    if audio_downgrade:
        # 降级的完整经过：触发机位、停了哪几路麦、停麦后到底救回来没有。
        # 没这些信息时，事后只能看到“某几路没音频”而无从判断原因。
        manifest["audio_downgrade"] = audio_downgrade
    return manifest


def print_summary(manifest: dict) -> None:
    print("\n=== 录制结果 ===")
    ok_rows = [s for s in manifest["sources"] if not s.get("error")]
    for s in manifest["sources"]:
        if s.get("error"):
            print(f"  ✗ {s['label']}：{s['error']}")
            continue
        off = s.get("start_offset_ms")
        off_txt = f"起点 +{off:.0f}ms" if off is not None else "起点未知"
        size = s.get("size_bytes") or 0
        extra = []
        if s.get("frames"):
            extra.append(f"{s['frames']} 帧")
        if s.get("size"):
            extra.append(s["size"])
        if s.get("fps_actual"):
            extra.append(f"实测 {s['fps_actual']:.1f}fps")
        elif s.get("fps"):
            extra.append(f"{s['fps']:.0f}fps")
        tail = f"（{', '.join(extra)}）" if extra else ""
        print(f"  ✓ {s['label']}：{Path(s['file']).name} "
              f"{size / 1e6:.1f} MB {tail} {off_txt}")
        if s.get("warn"):
            print(f"      [提示] {s['warn']}")
    max_off = manifest["alignment"]["max_offset_ms"]
    if max_off is not None and len(ok_rows) > 1:
        print(f"\n对齐：{len(ok_rows)} 路同录，最大起点差 {max_off:.0f}ms"
              f"（清单含逐源偏移，可据此裁齐）")
    for a in manifest.get("audio") or []:
        if a.get("error"):
            print(f"  ♫ {a['label']}：{a['error']}")
        else:
            dur = (a["samples"] or 0) / (a["sample_rate"] or 1)
            gap = f"，丢包 {a['seq_gaps']} 处" if a.get("seq_gaps") else ""
            dev = f"（{a['device_name']}）" if a.get("device_name") else ""
            print(f"  ♫ {a['label']}{dev}：{Path(a['file']).name} "
                  f"{dur:.1f}s @ {a['sample_rate']}Hz{gap}")
    dg = manifest.get("audio_downgrade")
    if dg:
        # 降级结论要当场就能看到：否则得翻清单才知道音频为何缺了。
        print(f"\n  ⚠ 音频降级（{dg['verdict']}）：{dg['note']}")
        print(f"      触发：{'、'.join(dg['trigger'])}读不到首帧"
              f"；停麦：{'、'.join(dg['mics_stopped']) or '无'}")
        if dg.get("cams_recovered"):
            print(f"      停麦后恢复：{'、'.join(dg['cams_recovered'])}")
        if dg.get("cams_still_failed"):
            print(f"      停麦后仍失败：{'、'.join(dg['cams_still_failed'])}")
        print(f"      重起回来的麦：{'、'.join(dg['mics_restarted']) or '无'}")


# ─── 主流程 ──────────────────────────────────────────────────────────────────


def _request_stop(reason: str) -> None:
    """提前结束：置位让 USB 拓帧循环跳出，并逐个叫后端 stop 让米家侧阻塞的
    ``record_clip`` 提前返回。

    **不能靠断开 HTTP 连接**：那会走后端的 cancel 分支、整段素材全丢。
    异常全吃：本函数可能在信号处理器里跑，抛异常会把主流程搞乱。
    """
    if _STOP_REQUESTED.is_set():
        return
    _STOP_REQUESTED.set()
    print(f"\n⬛ 提前结束（{reason}），各路收尾中…", flush=True)
    for cid, (u, tk) in list(_ACTIVE_CLIP_IDS.items()):
        try:
            stop_clip(u, tk, cid)
        except Exception as e:          # noqa: BLE001
            print(f"[警告] 通知后端收尾失败 {cid}: {e}", file=sys.stderr)


def _start_stop_watcher(stop_file: Path) -> threading.Thread:
    """轮询停止文件；它一出现就提前结束。

    为何用文件而不是信号：本工具典型是被 ``uv run`` 包起来跑的，``uv`` 不转发
    SIGUSR1，外层按 PID 发信号会发给包装进程而沉掉（实测踩过：信号发出去了、
    处理器根本未触发，录满了 60s）。文件不依赖进程拓扑，怎么包都有效。
    """

    def _poll() -> None:
        while not _STOP_REQUESTED.is_set():
            try:
                if stop_file.exists():
                    _request_stop(f"检测到 {stop_file.name}")
                    return
            except OSError:
                pass
            time.sleep(0.15)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t


def _install_stop_handler() -> None:
    """SIGUSR1 也能触发提前结束（直接跑本脚本时方便）。

    注意：经 ``uv run`` 启动时信号会被包装进程吃掉，那种场景请用
    ``--stop-file``。
    """
    try:
        signal.signal(signal.SIGUSR1,
                      lambda _s, _f: _request_stop("SIGUSR1"))
    except (ValueError, AttributeError):
        pass                            # 非主线程 / 不支持的平台


def run(args) -> None:
    need_miot = bool(args.miot or args.miot_all)
    need_usb = bool(args.usb or args.usb_auto)

    url, token = ("", "")
    cameras: list[dict] = []
    if need_miot or args.list:
        # 米家是可选源：显式要了（need_miot）就必须能连上，连不上硬失败；
        # 只是 --list 想看看有哪些源可用时则降级 —— 纯 USB 录制全程不碰后端，
        # 没道理因为后端没起、或这台机器压根没装过 miloco，连本机摄像头都列不出来。
        url, token = load_server_config(args.url, args.token, required=need_miot)
        if not token:
            print("[提示] 没有 miloco 后端 token，跳过米家摄像头（纯 USB 录制不需要）。")
        else:
            try:
                cameras = fetch_cameras(url, token)
            except ConnectError as e:
                if need_miot:
                    print(f"[错误] 连不上 miloco 后端 {url}（{e}）。", file=sys.stderr)
                    sys.exit(EXIT_NET)
                print(f"[提示] 连不上 miloco 后端 {url}，跳过米家摄像头。")
            except ApiError as e:
                if need_miot:
                    print(f"[错误] 获取摄像头列表失败（HTTP {e.status}）：{e}",
                          file=sys.stderr)
                    if hint := _hint_for_status(e.status):
                        print(f"  提示：{hint}", file=sys.stderr)
                    sys.exit(EXIT_API)
                print(f"[提示] 取米家摄像头列表失败（HTTP {e.status}），跳过米家摄像头。")

    if args.list:
        print_sources(cameras, url)
        return

    if not need_miot and not need_usb:
        print(
            "[错误] 没指定任何源。用 --miot / --miot-all 选米家摄像头，"
            "--usb / --usb-auto 选本机摄像头；--list 看可用项。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ARG)

    seconds = args.seconds
    if seconds <= 0:
        print("[错误] --seconds 需为正数。", file=sys.stderr)
        sys.exit(EXIT_ARG)
    # 2-60s 是后端 record_clip 的 duration_ms 限制，只约束米家源；纯 USB 是本机
    # 直采，多长都行。
    if need_miot and not (SEG_MIN_S <= seconds <= SEG_MAX_S):
        print(
            f"[错误] 带米家源时 --seconds 需在 {SEG_MIN_S}-{SEG_MAX_S} 之间"
            f"（后端单段录制上限 {SEG_MAX_S}s）。要更长素材请多跑几次；"
            f"本工具刻意不分段——分段会在每段之间留空隙，破坏同步连续性。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ARG)

    picked = resolve_miot_specs(cameras, args.miot, args.miot_all) if need_miot else []
    usb_indices = list(args.usb)
    if args.usb_auto:
        auto = pick_usb_auto()
        if not auto:
            print("[警告] --usb-auto 没枚举到可用的本机摄像头，跳过 USB。")
        for i in auto:
            if i not in usb_indices:
                usb_indices.append(i)

    # 一次录制 = 一个自包含的文件夹（以时间戳命名，天然按时间排序）。
    # 文件名不再带时间戳前缀，清单里也只存基名 —— 整个文件夹可整体搬迁/
    # 归档而不会断链（旧的平链布局下，一次 60s 录制会在目录里洒出 10 个文件）。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_dir = Path(args.out) / stamp
    clip_dir.mkdir(parents=True, exist_ok=True)
    out_dir = clip_dir

    # 未启用感知的米家相机：拉流会话是冷的，起点会滞后甚至 503。
    toggled: list[tuple[str, str]] = []  # (feed_did, label)
    cold = [c for c in picked if not c.get("in_use")]
    if cold:
        if args.ensure_in_use:
            for c in cold:
                cc = c.get("channel_count") or 1
                feed = (
                    f"{c['did']}:ch{c['channel']}" if cc > 1 else str(c["did"])
                )
                try:
                    set_camera_in_use(url, token, feed, True)
                    toggled.append((feed, str(c.get("name"))))
                except (ApiError, ConnectError) as e:
                    print(f"[错误] 临时启用 {feed} 失败：{e}", file=sys.stderr)
                    sys.exit(EXIT_API)
            print(f"已临时启用 {len(toggled)} 路未启用的相机，等待拉流会话就绪…")
            # 冷启 PPCS 会话 + 首个 IDR 通常 2-6s；等一会儿再同起，
            # 否则这几路的起点会明显落后于本来就热的相机。
            time.sleep(args.warmup)
        else:
            names = ", ".join(
                f"{c.get('did')}:ch{c.get('channel')}" for c in cold
            )
            print(
                f"[警告] 这些米家相机未启用感知（{names}）：拉流会话是冷的，"
                f"起点会明显滞后甚至录制失败。建议在家庭面板启用，"
                f"或加 --ensure-in-use 由本工具临时启用。"
            )

    # USB 自带麦克风必须在**视频流活跃之前**开：视频在采集时开同一复合设备的音频流会被
    # macOS 拒绝（PortAudio -9986，实测必现）。只提前麦克风：初始化毫秒级，不会像米家
    # WS + opus 那样拖累下面的预热测帧率。但反向也有坑：麦流活跃时同一 USB 集线器下的第二支
    # 摄像头可能不出帧（实测与分辨率无关）—— 所以下面若摄像头准备失败，会停麦降级重试。
    # 数据不会提前入文件：门控在栅栏放行时才开（见 audio_gate）。
    audio_gate = threading.Event()
    early_audio: EarlyAudio | None = None
    external_mic_cams: set[int] = set()  # 有活跃自带麦的外置摄像头（冲突风险源）
    if args.audio and usb_indices:
        try:
            early_audio = _start_usb_mic_capture(usb_indices, out_dir, audio_gate)
            cam_names = usb_device_names()
            external_mic_cams = {
                idx for idx in early_audio.active_cams
                if not _is_builtin_camera(
                    cam_names[idx] if idx < len(cam_names) else ""
                )
            }
        except Exception as e:
            print(f"[警告] USB 麦克风采集启动失败，本次仅录视频：{e}",
                  file=sys.stderr)

    # ---- 准备 USB 源（打开设备、实测帧率）----
    usb_sources: list[tuple[UsbSource, Result]] = []
    for idx in usb_indices:
        src = UsbSource(idx, args.width, args.height, seconds)
        names = usb_device_names()
        label = f"usb[{idx}] {names[idx] if idx < len(names) else ''}".strip()
        res = Result(kind="usb", label=label)
        try:
            src.setup()
        except Exception as e:  # 打不开 / 无权限 / 被占用
            res.error = str(e)
            res.extra["setup_failure_stage"] = getattr(e, "stage", "unknown")
            print(f"[警告] {label} 准备失败：{e}", file=sys.stderr)
            usb_sources.append((src, res))
            continue
        res.extra["size"] = f"{src.actual_w}x{src.actual_h}"
        usb_sources.append((src, res))

    # ---- 降级：USB 复合设备的麦流可能挤掉同一集线器下的其它摄像头 ----
    # 实测（两支 2MP USB Camera 接同一 hub）：麦流活跃时第二支摄像头完全不出帧，
    # 与分辨率/压缩格式无关（非纯带宽问题）；而内建麦（不在外接 hub 上）与多路视频
    # 共存正常。故只停掉 USB 复合设备的麦并重试失败的摄像头，内建麦保留。
    # 只有「设备成功打开、首帧读不到」与已活跃的外置复合麦同时出现，才有证据
    # 指向本 PR 针对的 hub 冲突。无效索引、权限/占用导致的 open 失败不能误伤
    # 其他正常机位的录音。具体判定与降级/回滚看 :func:`_resolve_mic_video_conflict`。
    conflict_failed = [
        (s, r) for s, r in usb_sources
        if r.extra.get("setup_failure_stage") == "first_frame"
    ]
    audio_downgrade: dict | None = None
    dropped_tracks: list = []
    if conflict_failed and early_audio is not None and external_mic_cams:
        early_audio, dropped_tracks, audio_downgrade = _resolve_mic_video_conflict(
            usb_sources, conflict_failed, early_audio, external_mic_cams,
            out_dir, audio_gate,
        )

    ready_usb = [(s, r) for s, r in usb_sources if r.error is None]
    n_parties = len(picked) + len(ready_usb)
    if n_parties == 0:
        print("[错误] 没有任何可用源。", file=sys.stderr)
        if early_audio is not None:
            _discard_early_audio(early_audio)
        _restore(url, token, toggled)
        sys.exit(EXIT_ARG)

    print(f"\n同步录制 {n_parties} 路，各 {seconds:g}s → {out_dir}/")
    for c in picked:
        cc = c.get("channel_count") or 1
        tag = f"{c['did']}:ch{c['channel']}" if cc > 1 else str(c["did"])
        print(f"  · 米家 {tag} {c.get('name') or ''}")
    for s, r in ready_usb:
        print(f"  · {r.label}（{s.actual_w}x{s.actual_h} @ {s.fps:.0f}fps）")

    # ---- 栅栏同起 ----
    # Barrier 的 action 在「最后一个参与者到达、即将全部放行」时跑一次，
    # 正好是**真正开始录制**的时刻，拿它打一条可被外部监控的标记。
    # 必须 flush：输出被重定向到文件/管道时 Python 走块缓冲，不 flush 则这行
    # 要等到进程退出才落盘，外部根本监不到「开始」（曾因此把语音报幕拖到
    # 录制快结束才响）。
    rec_started = threading.Event()

    def _on_barrier_release() -> None:
        print("▶ 开始录制（栅栏已放行）", flush=True)
        audio_gate.set()  # 早起的 USB 麦从此开始入文件（此前只滚动预卷）
        rec_started.set()

    barrier = threading.Barrier(n_parties, action=_on_barrier_release)
    threads: list[threading.Thread] = []
    results: list[Result] = []

    for c in picked:
        cc = c.get("channel_count") or 1
        tag = f"{c['did']}_ch{c['channel']}" if cc > 1 else str(c["did"])
        label = f"米家 {c.get('did')}:ch{c.get('channel')} {c.get('name') or ''}".strip()
        res = Result(kind="miot", label=label)
        res.extra["did"] = c.get("did")
        res.extra["channel"] = c.get("channel")
        results.append(res)
        out_path = out_dir / f"miot_{tag}.mp4"
        t = threading.Thread(
            target=miot_worker,
            args=(url, token, c, seconds, out_path, barrier, res),
            daemon=True,
        )
        threads.append(t)

    for s, r in ready_usb:
        results.append(r)
        out_path = out_dir / f"usb{s.index}.mp4"
        t = threading.Thread(
            target=s.run, args=(out_path, barrier, r), daemon=True
        )
        threads.append(t)

    # 准备失败的 USB 源也要进清单（带 error），但不参与栅栏
    results.extend(r for _, r in usb_sources if r.error is not None)

    print("\n各路准备中（USB 预热后统一放行）…", flush=True)
    if args.stop_file:
        _start_stop_watcher(Path(args.stop_file))
    for t in threads:
        t.start()
    # ⬇ 米家音频必须等栅栏放行后再起，不能提前：3 条 WS + opus 解码器
    # 同时初始化会把 USB 预热那 2.5s 的抓帧循环挤到极低（实测只剩 4fps），
    # 而写入帧率就是拿预热值定的 —— 文件会被写成 4fps、回放慢 6 倍。
    # USB 麦克风已在预热前提前开流（见 early_audio），这里只汇合。
    # 代价：米家音频少了开头约 0.3-0.5s 的前置覆盖，无碍（逐包时间戳自描述）。
    a_stop = a_threads = None
    a_tracks: list = []
    if args.audio or early_audio is not None:
        rec_started.wait(timeout=90)
        try:
            a_stop, a_threads, a_tracks = start_audio_capture(
                url, token, picked, out_dir, early_audio
            )
            print(f"  音频采集已起（{len(a_tracks)} 路）", flush=True)
        except Exception as e:
            print(f"[警告] 音频采集启动失败，仅录视频：{e}", file=sys.stderr)
            a_stop, a_threads, a_tracks = None, None, []
    # 超时上限：时长 + 冷启/编码/落盘余量；线程内部自己也有超时。
    deadline = time.monotonic() + seconds + 40
    try:
        for t in threads:
            t.join(timeout=max(1.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，等各路收尾…")
        for t in threads:
            t.join(timeout=5)

    for s, _ in usb_sources:
        s.close()
    if a_stop is not None:
        # 音频比视频晚一拍停：确保视频最后一帧对应的声音也收进去。
        time.sleep(0.3)
        a_stop.set()
        for t in (a_threads or []):
            t.join(timeout=15)
    _restore(url, token, toggled)

    # 被降级停掉、最终也没起回来的机位：并进清单，别让它凭空消失
    live_mics = {tr.label for tr in (a_tracks or [])}
    a_tracks = list(a_tracks or []) + [
        tr for tr in dropped_tracks if tr.label not in live_mics]

    manifest = build_manifest(results, seconds, a_tracks or None, audio_downgrade)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print_summary(manifest)
    print(f"\n清单（含逐源起点与偏移）：{manifest_path}")

    if not [s for s in manifest["sources"] if not s.get("error")]:
        print("[错误] 所有源都失败了。", file=sys.stderr)
        sys.exit(EXIT_API)


def _restore(url: str, token: str, toggled: list[tuple[str, str]]) -> None:
    """把 --ensure-in-use 临时启用的相机关回原状（尽力而为）。"""
    for feed, _name in toggled:
        try:
            set_camera_in_use(url, token, feed, False)
            print(f"已恢复 {feed} 为未启用状态。")
        except (ApiError, ConnectError) as e:
            print(
                f"[警告] 恢复 {feed} 未启用状态失败（{e}），请到家庭面板手动关闭。",
                file=sys.stderr,
            )


def main() -> None:
    _install_stop_handler()
    p = argparse.ArgumentParser(
        description="多摄像头同步录制（本机摄像头 + 可选米家多路，时间对齐）"
    )
    p.add_argument("--list", action="store_true", help="列出可用源后退出")
    p.add_argument(
        "--miot",
        action="append",
        default=[],
        metavar="SPEC",
        help="米家摄像头：--list 的序号 / did / did:ch{n}；可重复",
    )
    p.add_argument("--miot-all", action="store_true", help="选中全部米家摄像头通道")
    p.add_argument(
        "--usb",
        action="append",
        type=int,
        default=[],
        metavar="INDEX",
        help="本机摄像头索引（--list 可查）；可重复",
    )
    p.add_argument(
        "--usb-auto", action="store_true",
        help="自动选本机摄像头（外置 + 内置，跳过 iPhone/iPad 连续互通）",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=15.0,
        help=f"录制时长（秒），默认 15；带米家源时受后端限制在 {SEG_MIN_S}-{SEG_MAX_S}",
    )
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "sync_clips"),
                   help="输出目录，默认脚本所在目录下的 sync_clips/")
    p.add_argument("--width", type=int, default=1280, help="USB 请求宽度，默认 1280")
    p.add_argument("--height", type=int, default=720, help="USB 请求高度，默认 720")
    p.add_argument(
        "--ensure-in-use",
        action="store_true",
        help="临时启用未启用感知的米家相机（录完关回）",
    )
    p.add_argument(
        "--audio",
        action="store_true",
        help="同时录音（米家走音频流、USB 走自带麦克风），输出 WAV + 逐包时间戳。"
             "需额外依赖 av / websockets / sounddevice，且米家相机需已开拾音",
    )
    p.add_argument(
        "--stop-file",
        help="轮询该路径；文件一出现就提前结束录制（保留已录内容）。"
             "经 uv run 启动时请用它而不是 SIGUSR1，uv 不转发信号",
    )
    p.add_argument(
        "--warmup",
        type=float,
        default=6.0,
        help="--ensure-in-use 后等待拉流会话就绪的秒数，默认 6",
    )
    p.add_argument("--url", help=f"miloco 后端地址，默认 {DEFAULT_URL}（或读配置）")
    p.add_argument("--token", help="Bearer Token，默认读 $MILOCO_HOME/config.json")
    args = p.parse_args()

    if args.warmup < 0:
        p.error("--warmup 不能为负")
    run(args)


if __name__ == "__main__":
    main()
