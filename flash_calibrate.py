#!/usr/bin/env python3
"""闪光延迟标定：量出每路摄像头的**视频**链路延迟（画面到达主机 − 事件真实发生）。

为什么必须用「视觉」刺激而不是蜂鸣
--------------------------------
蜂鸣（见 ``beep_calibrate.py``）测的是**音频**链路延迟。实测发现两者并不等价：
某台云台机的音频延迟 128ms、视频延迟却是 655ms（差 5 倍），而双摄机与 USB 摄像头
上两者接近。所以要校正**画面**对齐，只能用画面里能看见的刺激。

为什么闪光间隔必须随机
--------------------
等间隔闪光会造成周期性错配：间隔 1502ms 时，真实延迟 L 与 L+1502ms 无法区分
（实测踩过：真值 655ms 被错配成 151ms）。随机间隔让「整体延迟」只有唯一解。

原理
----
本机全屏黑白闪烁，每次变白记下**主机时刻**；各路录像里找亮度上升沿，则

    视频延迟 = 上升沿在该路录像时间轴上的位置 − 对应闪光的主机时刻

录像时间轴来自 ``first_frame_unix_ms + PTS``（PTS 即帧到达主机的真实偏移），
全程只用主机时钟，**不读相机 RTC**（相机时钟实测偏差约 0.3s，不可作基准）。

用法（关灯效果更好；需所有相机都能看见本机屏幕）
    uv run flash_calibrate.py --seconds 40 --out latency.json
    uv run flash_calibrate.py --repeat 3        # 重复多轮，看可复现性
    uv run flash_calibrate.py --miot            # 连米家摄像头一起标（需 miloco 后端）

默认只标本机 USB 摄像头；米家摄像头是可选项，要标它得加 ``--miot``。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

import av
import numpy as np

WHITE_MS = 400
GAP_MIN_MS, GAP_MAX_MS = 2000, 4500     # 随机间隔，破除周期性错配
BLOCKS = 16                             # 画面分块数（自动挑对闪光响应最强的块）
                                        # 8x8 太粗：屏幕只占画面一小块时信号被均掉

# 用 __file__ 推导而非裸文件名：本脚本可能从任意 CWD 启动
RECORDER = Path(__file__).resolve().parent / "multi_cam_recorder.py"


class Flasher:
    """tkinter 全屏黑白闪烁；每次变白记下主机时刻。

    放在主线程跑（macOS 的 Tk 要求），录制与分析交给子进程/后续步骤。
    """

    def __init__(self, duration_s: float):
        self.duration_s = duration_s
        self.stamps: list[float] = []
        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.title("flash-calibrate")
        self._t_end = time.time() + duration_s

    def _white(self) -> None:
        if time.time() > self._t_end:
            self.root.quit()
            return
        self.root.configure(bg="white")
        # update() 后才真正上屏，此刻打点最接近实际显示时刻
        self.root.update_idletasks()
        self.root.update()
        self.stamps.append(time.time())
        self.root.after(WHITE_MS, self._black)

    def _black(self) -> None:
        self.root.configure(bg="black")
        self.root.update_idletasks()
        self.root.after(random.randint(GAP_MIN_MS, GAP_MAX_MS), self._white)

    def run(self) -> None:
        self.root.after(300, self._white)
        self.root.mainloop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _resolve(manifest_path: Path, ref: str | None) -> Path | None:
    """把清单里的文件引用解成实际路径。

    新布局下清单只存基名（与素材同居一个 clip 文件夹）；旧布局存的是仓库
    相对路径。两者都取基名后按「清单所在目录」解析 —— 旧文件也就在清单旁边，
    所以一套逻辑同时兼容新旧。
    """
    if not ref:
        return None
    p = manifest_path.parent / Path(ref).name
    return p if p.exists() else None


def _frame_times(
    manifest_path: Path, s: dict, n_decoded: int, pts_ms: list[float]
) -> np.ndarray | None:
    """算出每帧的**主机时刻**（unix ms）。

    必须分两种情况，否则 USB 那路会错得很难看：

    * 米家：mp4 的 PTS 就是帧到达主机的真实偏移（后端 NalClipRecorder 按
      recv_unix_ms 打的），直接 first + PTS 即可。
    * USB：VideoWriter 只能按**固定名义帧率**写 CFR，而实际采集帧率与它差
      几个百分点 —— 用 PTS 推时刻会随时长线性漂移（35s 上约 1.7s），闪光
      匹配直接崩掉（实测相关性从 0.95 跌到 0.18）。录制工具为此落了逐帧
      时间戳 sidecar（``*_frames.csv``），这里必须优先用它。
    """
    csv_path = _resolve(manifest_path, s.get("frame_timestamps"))
    if csv_path:
        rows = []
        for line in csv_path.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) == 2:
                try:
                    rows.append(float(parts[1]))
                except ValueError:
                    pass
        if len(rows) >= 10:
            k = min(len(rows), n_decoded)
            return np.array(rows[:k], dtype=float)
    first = s.get("first_frame_unix_ms")
    if first is None:
        return None
    return np.array([first + p for p in pts_ms], dtype=float)


def analyze(manifest_path: Path, flashes: np.ndarray) -> dict:
    """在每路录像里定位闪光上升沿，解出该路视频延迟。"""
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = {}
    for s in m["sources"]:
        if s.get("error") or not s.get("file"):
            continue
        vpath = _resolve(manifest_path, s.get("file"))
        if vpath is None:
            result[s["label"]] = {"status": "file_missing", "ref": s.get("file")}
            continue
        c = av.open(str(vpath))
        pts_ms, blocks = [], []
        for pk in c.demux(video=0):
            for f in pk.decode():
                g = f.to_ndarray(format="gray").astype(np.float32)
                h, w = g.shape
                bh, bw = h // BLOCKS, w // BLOCKS
                blk = g[: bh * BLOCKS, : bw * BLOCKS].reshape(
                    BLOCKS, bh, BLOCKS, bw
                ).mean(axis=(1, 3))
                blocks.append(blk.ravel())
                pts_ms.append(float(f.pts) * float(f.time_base) * 1000)
        if len(pts_ms) < 20:
            continue
        ts = _frame_times(manifest_path, s, len(pts_ms), pts_ms)
        if ts is None:
            continue
        B = np.array(blocks[: len(ts)])             # (帧, 块)
        ts = ts[: len(B)]

        # 先粗搜整体延迟：用「块亮度 vs 闪光方波」的相关性挑出屏幕所在的块。
        # 整幅均值会被自动曝光爬升和场景变化污染（实测 USB 那路峰峰 370ms），
        # 只取响应最强的局部块能显著提高信噪比。
        best = (None, -1.0, None)                  # (延迟, 相关性, 块号)
        for lag in np.arange(0, 2600, 20.0):
            ref = np.zeros(len(ts))
            for fl in flashes:
                ref += ((ts - lag >= fl) & (ts - lag < fl + WHITE_MS)).astype(float)
            if ref.std() < 1e-9:
                continue
            for j in range(B.shape[1]):
                col = B[:, j]
                if col.std() < 1e-6:
                    continue
                cc = float(np.corrcoef(col, ref)[0, 1])
                if cc > best[1]:
                    best = (float(lag), cc, j)
        lag0, cc, jbest = best
        if jbest is None or cc < 0.3:
            result[s["label"]] = {"status": "no_flash_signal", "corr": cc}
            continue

        # 用最强块的亮度序列精算逐次上升沿（亚帧线性插值）
        lum = B[:, jbest]
        d = np.diff(lum)
        thr = max(1.0, 4 * float(np.median(np.abs(d - np.median(d)))))
        groups: list[list[int]] = []
        for i in np.flatnonzero(d > thr):
            if groups and i - groups[-1][-1] <= 2:
                groups[-1].append(int(i))
            else:
                groups.append([int(i)])
        edges = []
        for g in groups:
            i = g[0]
            hi = lum[min(g[-1] + 1, len(lum) - 1)]
            half = lum[i] + 0.5 * (hi - lum[i])
            denom = lum[i + 1] - lum[i]
            frac = float(np.clip((half - lum[i]) / denom, 0, 1)) if denom else 0.5
            edges.append(ts[i] + frac * (ts[i + 1] - ts[i]))

        lat = []
        for e in edges:
            k = int(np.argmin(np.abs(flashes - (e - lag0))))
            dt = e - flashes[k]
            if abs(dt - lag0) < 300:               # 只保留贴合粗搜解的
                lat.append(dt)
        if len(lat) < 4:
            result[s["label"]] = {"status": "too_few_edges", "n": len(lat),
                                  "corr": cc}
            continue
        lat = np.array(lat)
        result[s["label"]] = {
            "status": "ok",
            "kind": s.get("kind"),
            "n_flashes": int(len(lat)),
            "corr": round(cc, 3),
            "block": int(jbest),
            "video_latency_ms": round(float(np.median(lat)), 1),
            "q25_ms": round(float(np.percentile(lat, 25)), 1),
            "q75_ms": round(float(np.percentile(lat, 75)), 1),
            "spread_ms": round(float(lat.max() - lat.min()), 1),
        }
    return result


def one_round(args, rnd: int) -> dict:
    """一轮：起录制 → 闪烁 → 等录完 → 分析。"""
    out_dir = Path(args.work) / f"round{rnd}"
    log = Path(args.work) / f"round{rnd}_rec.txt"
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = [sys.executable, str(RECORDER), "--usb-auto",
           "--seconds", str(args.seconds), "--out", str(out_dir)]
    if args.miot:
        cmd.append("--miot-all")
    with open(log, "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)

    # 等栅栏放行（真正开始录制）再开始闪，避免闪光落在预热阶段
    t0 = time.time()
    started = False
    while time.time() - t0 < 90 and proc.poll() is None:
        if log.exists() and "栅栏已放行" in log.read_text(errors="ignore"):
            started = True
            break
        time.sleep(0.2)
    if not started:
        proc.wait(timeout=args.seconds + 60)
        print(f"[轮 {rnd}] 录制未能开始，日志见 {log}", file=sys.stderr)
        return {}

    # 闪烁时长略短于录制余量，确保上升沿都落在录像内
    fl = Flasher(duration_s=max(5.0, args.seconds - 4))
    fl.run()
    proc.wait(timeout=args.seconds + 60)

    mans = sorted(out_dir.glob("*_manifest.json")) + sorted(out_dir.glob("*/manifest.json"))
    if not mans:
        print(f"[轮 {rnd}] 没有产出清单", file=sys.stderr)
        return {}
    mans.sort(key=lambda p: p.stat().st_mtime)
    flashes = np.array(fl.stamps, dtype=float) * 1000
    print(f"[轮 {rnd}] 闪光 {len(flashes)} 次，分析中…")
    return analyze(mans[-1], flashes)


def main() -> None:
    p = argparse.ArgumentParser(description="闪光视频延迟标定")
    p.add_argument("--seconds", type=int, default=40, help="每轮录制秒数，默认 40")
    p.add_argument("--repeat", type=int, default=1, help="重复轮数（看可复现性）")
    p.add_argument("--work", default="/tmp/flashcal", help="中间产物目录")
    p.add_argument("--out", help="把标定结果写入该 JSON")
    p.add_argument("--miot", action="store_true",
                   help="连米家摄像头一起标定（需 miloco 后端可达）；默认只标本机 USB")
    args = p.parse_args()

    rounds = []
    for r in range(1, args.repeat + 1):
        res = one_round(args, r)
        if res:
            rounds.append(res)

    if not rounds:
        print("[错误] 没有任何有效轮次。", file=sys.stderr)
        sys.exit(3)

    labels = sorted({k for r in rounds for k in r})
    print(f"\n{'源':24s} " + " ".join(f"{'轮'+str(i+1):>9s}" for i in range(len(rounds)))
          + f" {'中位':>9s} {'轮间极差':>9s}")
    print("-" * (26 + 10 * len(rounds) + 20))
    summary = {}
    for lb in labels:
        vals = []
        cells = []
        for r in rounds:
            e = r.get(lb, {})
            if e.get("status") == "ok":
                vals.append(e["video_latency_ms"])
                cells.append(f"{e['video_latency_ms']:8.0f}m")
            else:
                cells.append(f"{e.get('status','-')[:9]:>9s}")
        if vals:
            med = float(np.median(vals))
            rng = max(vals) - min(vals)
            summary[lb] = {"video_latency_ms": round(med, 1),
                           "rounds_ms": vals,
                           "between_round_range_ms": round(rng, 1)}
            print(f"  {lb[:22]:22s} " + " ".join(cells) +
                  f" {med:8.0f}m {rng:8.0f}m")
        else:
            print(f"  {lb[:22]:22s} " + " ".join(cells) + "        -         -")

    print("\n轮间极差小 → 延迟稳定、一次标定可长期复用；")
    print("轮间极差大 → 需每次录制现场标定（或首尾各标一次做插值）。")
    if args.out:
        Path(args.out).write_text(
            json.dumps({"measured_at": time.time(), "n_rounds": len(rounds),
                        "per_source": summary, "raw_rounds": rounds},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
