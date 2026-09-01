#!/usr/bin/env python3
"""蜂鸣延迟标定：量出每路摄像头「画面/声音到达主机」相对「事件真实发生」的延迟。

为什么需要它
------------
多机同录时，各路的时间锚点是「首帧**到达主机**的时刻」。而米家摄像头的一帧要走
「相机编码 → PPCS 网络传输 → 主机解码」才到达（实测 0.5–1s），USB 摄像头是本机
直连（几十毫秒）。若都按到达时刻对齐，米家画面就被整体推后了它的传输延迟 ——
同一全局时刻，USB 呈现的是更晚发生的事，看起来"更快"。

标定原理
--------
本机在**已知主机时刻**播放三声蜂鸣，各路（米家走音频流、USB 走自带麦克风）都能
听到；在各路音频里检出蜂鸣起点，则

    该链路延迟 = 蜂鸣在该路音频中到达主机的时刻 − 蜂鸣真实发出的主机时刻

全程只用主机自己的时钟打点，**不读任何相机的 RTC**，所以相机时钟误差不参与
（这正是它优于"读画面水印"那种办法的地方 —— 水印法测出的是「延迟 + 相机时钟
误差」，两者无法分离）。

用法
----
依赖由本子库的 ``pyproject.toml`` 声明，首次先 ``uv sync`` 建环境，之后：

    uv run beep_calibrate.py --list-mics
    uv run beep_calibrate.py --usb-mic 2

米家是可选源：miloco 后端不可达时自动降级为只标本机麦克风，此时必须给
``--usb-mic``（不然没有可标的源）。

前提：米家相机需已启用感知（拉流会话在跑）且**已打开拾音**（voice_in_use），
否则音频流没有数据。双摄机型通常只有 ch0 挂麦克风，ch1 无音频 —— 但两者同属
一台设备、共用一条链路，可套用 ch0 的结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import av
import numpy as np
import sounddevice as sd
import websockets

BEEP_HZ = 3000.0           # 各路采样率≥16k(Nyquist≥8k)，3k 安全且小麦克风灵敏
BEEP_MS, GAP_MS, N_BEEP = 120, 180, 3
LEAD_S = 1.2               # 播放前导静音：让各路采集先跑起来
CAPTURE_S = 6.0

DEFAULT_URL = "http://127.0.0.1:1810"


# ── 蜂鸣 ────────────────────────────────────────────────────────────────────


def make_beep(sr: int) -> np.ndarray:
    """前导静音 + 三声加窗正弦。加窗是为了避免方波爆音的宽带瞬态干扰检测。"""
    lead = np.zeros(int(LEAD_S * sr), dtype=np.float32)
    n = int(BEEP_MS / 1000 * sr)
    tone = (0.9 * np.sin(2 * np.pi * BEEP_HZ * np.arange(n) / sr)).astype(np.float32)
    tone *= np.hanning(n).astype(np.float32)
    gap = np.zeros(int(GAP_MS / 1000 * sr), dtype=np.float32)
    parts = [lead]
    for i in range(N_BEEP):
        parts.append(tone)
        if i < N_BEEP - 1:
            parts.append(gap)
    parts.append(np.zeros(int(0.3 * sr), dtype=np.float32))
    return np.concatenate(parts)


class Beeper:
    """播放蜂鸣并记下第一声真实发出的主机时刻。"""

    def __init__(self) -> None:
        self.onset_host: float | None = None

    def run(self) -> None:
        sr = 48000
        wav = make_beep(sr)
        st = sd.OutputStream(samplerate=sr, channels=1, dtype="float32")
        st.start()
        # PortAudio 报告的输出延迟要计入：送样时刻 + 前导静音 + 输出延迟
        out_lat = float(st.latency)
        self.onset_host = time.time() + LEAD_S + out_lat
        st.write(wav)
        st.stop()
        st.close()


# ── 采集 ────────────────────────────────────────────────────────────────────


async def collect_miot(url_ws: str, label: str, out: dict) -> None:
    """米家音频流（WebSocket + opus）。每个解码块记下其到达主机的时刻。"""
    dec = av.CodecContext.create("opus", "r")
    dec.sample_rate = 48000
    dec.layout = "mono"
    pcm_parts: list[np.ndarray] = []
    stamps: list[tuple[float, int]] = []
    t0 = time.time()
    try:
        async with websockets.connect(url_ws, max_size=None) as ws:
            while time.time() - t0 < CAPTURE_S:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, CAPTURE_S - (time.time() - t0))
                    )
                except Exception:
                    break
                if isinstance(msg, str):
                    continue
                arrive = time.time()
                try:
                    for fr in dec.decode(av.Packet(msg)):
                        a = fr.to_ndarray().astype(np.float32).ravel()
                        if fr.format.name.startswith("s16"):
                            a = a / 32768.0
                        pcm_parts.append(a)
                        stamps.append((arrive, len(a)))
                except Exception as e:
                    out[label] = ("decode_error", f"{type(e).__name__}: {e}")
                    return
    except Exception as e:
        out[label] = ("ws_error", f"{type(e).__name__}: {e}")
        return
    if not pcm_parts:
        out[label] = ("no_audio", "拾音未开启 / 该通道无麦克风")
        return
    out[label] = ("ok", (np.concatenate(pcm_parts), stamps, 48000))


def collect_mic(dev: int, label: str, out: dict) -> None:
    """本机麦克风（USB 摄像头自带麦）。每个回调块记下到达时刻。"""
    info = sd.query_devices(dev)
    sr = int(info["default_samplerate"])
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, time_info, status):
        q.put((time.time(), indata[:, 0].copy()))

    try:
        with sd.InputStream(device=dev, channels=1, samplerate=sr,
                            dtype="float32", callback=cb, blocksize=512):
            time.sleep(CAPTURE_S)
    except Exception as e:
        out[label] = ("mic_error", f"{type(e).__name__}: {e}")
        return
    parts, stamps = [], []
    while not q.empty():
        ts, b = q.get()
        parts.append(b)
        stamps.append((ts, len(b)))
    if not parts:
        out[label] = ("no_audio", "麦克风无数据")
        return
    out[label] = ("ok", (np.concatenate(parts), stamps, sr))


# ── 检测 ────────────────────────────────────────────────────────────────────


def detect_beep(pcm: np.ndarray, stamps: list[tuple[float, int]], sr: int):
    """在 BEEP_HZ 处做窄带能量包络，找第一声蜂鸣起点。

    返回 ``(起点的主机时刻, 信噪比dB, 检出脉冲个数)``；未检出则起点为 None。
    与 BEEP_HZ 复混频再平滑，等价于单频匹配滤波，对宽带室内噪声抑制很好。
    """
    n = len(pcm)
    mixed = pcm * np.exp(-2j * np.pi * BEEP_HZ * np.arange(n) / sr)
    win = max(8, int(0.010 * sr))
    env = np.abs(np.convolve(mixed, np.ones(win) / win, mode="same"))

    noise = float(np.median(env))
    peak = float(env.max())
    snr = 20 * np.log10(peak / (noise + 1e-12))
    thr = noise + 0.35 * (peak - noise)
    above = env > thr
    if not above.any():
        return None, snr, 0

    cuts = np.flatnonzero(np.diff(above.astype(int)) != 0) + 1
    segs = np.split(np.arange(n), cuts)
    min_len = 0.4 * BEEP_MS / 1000 * sr        # 至少 40% 蜂鸣时长，滤掉毛刺
    bursts = [s for s in segs if len(s) and above[s[0]] and len(s) > min_len]
    if not bursts:
        return None, snr, 0

    onset = int(bursts[0][0])
    # 采样号 → 主机时刻。块的到达时刻对应该块**末尾**，故要回退该块时长。
    cum = 0
    for arrive, cnt in stamps:
        if cum + cnt > onset:
            block_start = arrive - cnt / sr
            return block_start + (onset - cum) / sr, snr, len(bursts)
        cum += cnt
    return None, snr, len(bursts)


# ── 主流程 ──────────────────────────────────────────────────────────────────


def resolve_mic(spec: str) -> int:
    """把设备指定（索引或名字片段）解成 PortAudio 设备索引。

    不能只支持索引：PortAudio 的设备索引会因设备插拔、甚至同一台相机的
    视频被占用而变动（实测：同一个索引 2 上一轮是 USB 麦克风、下一轮变成
    其它设备，报 Invalid number of channels）。名字匹配才稳。
    """
    devs = sd.query_devices()
    if spec.isdigit():
        i = int(spec)
        if 0 <= i < len(devs) and devs[i]["max_input_channels"] > 0:
            return i
        raise SystemExit(f"[错误] 设备 [{i}] 不是可用输入设备，跑 --list-mics 看看。")
    low = spec.lower()
    hits = [i for i, d in enumerate(devs)
            if d["max_input_channels"] > 0 and low in d["name"].lower()]
    if not hits:
        raise SystemExit(f"[错误] 没找到名字含 {spec!r} 的输入设备，跑 --list-mics 看看。")
    return hits[0]


def load_token(url_arg, token_arg) -> tuple[str, str]:
    url, token = url_arg, token_arg
    if not (url and token):
        home = Path(os.environ.get("MILOCO_HOME") or Path.home() / ".openclaw/miloco")
        try:
            cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
            srv = cfg.get("server") or {}
            url = url or srv.get("url")
            token = token or srv.get("token")
        except (OSError, json.JSONDecodeError):
            pass
    return (url or DEFAULT_URL).rstrip("/"), (token or "")


def fetch_cameras(url: str, token: str) -> list[dict]:
    import urllib.request

    req = urllib.request.Request(f"{url}/api/miot/scope/cameras")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["data"]


async def run(args) -> int:
    url, token = load_token(args.url, args.token)
    mic_dev = resolve_mic(args.usb_mic) if args.usb_mic else None

    # 米家是可选源：后端不可达 / 没 token 时降级为只标本机麦克风，
    # 而不是抛 traceback —— 纯 USB 机位也要能量出自己那条链路的延迟。
    cams: list[dict] = []
    if token:
        try:
            cams = [c for c in fetch_cameras(url, token) if c.get("in_use")]
        except (OSError, ValueError, KeyError) as e:
            print(f"[提示] 取不到米家摄像头（{e}），只标本机麦克风。")
    else:
        print("[提示] 没有 miloco 后端 token，只标本机麦克风。")
    if not cams and mic_dev is None:
        print("[错误] 没有可标定的源：米家不可用时须用 --usb-mic 指定本机麦克风"
              "（--list-mics 看可用项）。", file=sys.stderr)
        return 2
    ws_base = url.replace("http://", "ws://").replace("https://", "wss://")

    targets = []
    for c in cams:
        label = f"{c['did']}:ch{c['channel']}"
        targets.append((
            label,
            f"{ws_base}/api/miot/ws/audio_stream"
            f"?camera_id={c['did']}&channel={c['channel']}&token={token}",
            c,
        ))

    print(f"标定 {len(targets)} 路米家音频" +
          (f" + 本机麦克风[{mic_dev}] {sd.query_devices(mic_dev)['name']}"
           if mic_dev is not None else ""))
    for lb, _, c in targets:
        print(f"  · {lb} {c.get('name','')} 拾音={'开' if c.get('voice_in_use') else '关'}")
    print(f"\n将播放 {N_BEEP} 声 {BEEP_HZ/1000:g}kHz 蜂鸣（约 "
          f"{(BEEP_MS*N_BEEP + GAP_MS*(N_BEEP-1))/1000:.1f}s），请保持环境安静…\n")

    out: dict = {}
    tasks = [asyncio.create_task(collect_miot(u, lb, out)) for lb, u, _ in targets]
    threads = []
    if mic_dev is not None:
        t = threading.Thread(target=collect_mic,
                             args=(mic_dev, "本机麦克风", out), daemon=True)
        t.start()
        threads.append(t)

    beeper = Beeper()
    await asyncio.sleep(0.6)                    # 等采集起来
    tb = threading.Thread(target=beeper.run, daemon=True)
    tb.start()
    threads.append(tb)

    await asyncio.gather(*tasks)
    for t in threads:
        t.join(timeout=CAPTURE_S + 2)

    if beeper.onset_host is None:
        print("[错误] 蜂鸣未能播放（检查音频输出设备）。", file=sys.stderr)
        return 3

    print(f"{'源':16s} {'状态':10s} {'脉冲':>5s} {'信噪比':>9s} {'链路延迟':>10s}")
    print("-" * 60)
    result = {}
    labels = [lb for lb, _, _ in targets] + (
        ["本机麦克风"] if mic_dev is not None else [])
    for lb in labels:
        st, payload = out.get(lb, ("missing", None))
        if st != "ok":
            print(f"  {lb:14s} {st:10s} {'-':>5s} {'-':>9s} {'-':>10s}  "
                  f"{payload or ''}")
            continue
        pcm, stamps, sr = payload
        onset, snr, nb = detect_beep(pcm, stamps, sr)
        if onset is None:
            print(f"  {lb:14s} {'有数据':10s} {nb:5d} {snr:8.1f}dB {'未检出':>10s}")
        else:
            lat = (onset - beeper.onset_host) * 1000
            result[lb] = lat
            print(f"  {lb:14s} {'有数据':10s} {nb:5d} {snr:8.1f}dB {lat:9.0f}ms")

    print("\n延迟 = 蜂鸣在该路音频中到达主机的时刻 − 蜂鸣发出的主机时刻")
    print("（全程主机时钟，不含相机 RTC 误差）")
    if args.out and result:
        Path(args.out).write_text(
            json.dumps({"measured_at": time.time(), "beep_hz": BEEP_HZ,
                        "latency_ms": result}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="蜂鸣延迟标定")
    p.add_argument("--list-mics", action="store_true", help="列出本机音频输入设备后退出")
    p.add_argument("--usb-mic", help="USB 摄像头麦克风：设备索引或名字片段（推荐名字，索引会变）")
    p.add_argument("--out", help="把标定结果写入该 JSON 文件")
    p.add_argument("--url", help=f"miloco 后端，默认 {DEFAULT_URL}（或读配置）")
    p.add_argument("--token", help="Bearer Token，默认读 $MILOCO_HOME/config.json")
    args = p.parse_args()

    if args.list_mics:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']}  in={d['max_input_channels']} "
                      f"sr={d['default_samplerate']:.0f}")
        return
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
