#!/usr/bin/env python3
"""USB 外接摄像头录视频小工具（基于 OpenCV）。

依赖由本子库的 ``pyproject.toml`` 声明，首次先 ``uv sync`` 建环境，之后：

    # 1) 列出摄像头，并给每个索引存一张样张 cam_<i>.jpg，看图确认哪个是哪个
    uv run usb_cam_recorder.py --list

    # 2) 直接录制（自动选择名字含 USB 的摄像头），预览窗口按 q / Esc 停止
    uv run usb_cam_recorder.py

    # 3) 指定索引、分辨率、最长时长，不带预览窗口后台录制
    uv run usb_cam_recorder.py --camera 0 \
        --width 1920 --height 1080 --max-seconds 60 --no-preview
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

WINDOW = "REC - press q to stop"
PROBE_RANGE = range(4)  # 最多探测的摄像头索引


def macos_camera_names():
    """调 system_profiler 拿到相机名列表（仅 macOS，失败返回空表）。"""
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        # 设备名行是 4 空格缩进、以冒号结尾，属性行缩进更深
        if line.startswith("    ") and not line.startswith("        ") and line.strip().endswith(":"):
            names.append(line.strip()[:-1].strip())
    return names


def avfoundation_devices():
    """用与 OpenCV 完全相同的方式枚举摄像头，返回 (名字列表, 是否严格一致)。

    OpenCV 5.x 的 AVFoundation 后端：先用 [AVCaptureDevice devicesWithMediaType:]
    取视频设备列表，再按 uniqueID 升序排序（保证顺序跨启动稳定）。本函数
    复现同样的两步，得到的顺序就是 cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    里 index 的真实含义（system_profiler 的顺序与它无关，可能完全相反）。
    需要 pyobjc-framework-AVFoundation，不可用时返回 ([], False) 走降级逻辑。
    """
    if sys.platform != "darwin":
        return [], False
    try:
        from AVFoundation import AVCaptureDevice
    except ImportError:
        return [], False
    try:
        devices = list(AVCaptureDevice.devicesWithMediaType_("vide"))  # AVMediaTypeVideo
        devices.sort(key=lambda d: str(d.uniqueID()))  # 与 OpenCV 相同的排序键
        return [str(d.localizedName()) for d in devices], True
    except Exception:
        pass
    # 兜底：devicesWithMediaType 属 deprecated API，未来系统若移除，退回
    # discovery session。注意它的结果未经排序，与 OpenCV 索引可能不一致。
    try:
        from AVFoundation import AVCaptureDeviceDiscoverySession
        session = AVCaptureDeviceDiscoverySession.pyobjc_classMethods.discoverySessionWithDeviceTypes_mediaType_position_(
            ["AVCaptureDeviceTypeBuiltInWideAngleCamera", "AVCaptureDeviceTypeExternal"],
            "vide",
            0,
        )
        return [str(d.localizedName()) for d in session.devices()], False
    except Exception:
        return [], False


def camera_names_for_index():
    """返回 (设备名列表, 来源说明)。优先 AVFoundation（与索引严格一致）。"""
    names, exact = avfoundation_devices()
    if names:
        if exact:
            return names, "AVFoundation（复现 OpenCV 的枚举+排序，与索引严格一致）"
        return names, "AVFoundation discovery session（未排序，仅供参考）"
    return macos_camera_names(), "system_profiler（顺序不可靠，仅供参考）"


def open_camera(index):
    """打开指定索引的摄像头，macOS 上明确走 AVFoundation 后端。"""
    if sys.platform == "darwin":
        return cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    return cv2.VideoCapture(index, cv2.CAP_ANY)


def resolve_camera(arg_index):
    """确定要用的摄像头索引：手动指定优先，否则按名字自动挑 USB 相机。"""
    if arg_index is not None:
        return arg_index, "(手动指定)"
    names, _ = camera_names_for_index()
    for i, name in enumerate(names):
        if "usb" in name.lower() or "uvc" in name.lower():
            print(f"自动选择摄像头 [{i}] {name}（名字含 USB）。")
            print("不确定的话先跑 --list 看各索引的样张确认。")
            return i, name
    print("未找到名字含 USB 的摄像头，默认使用 [0]。")
    return 0, names[0] if names else ""


def measure_fps(cap, frames_to_measure=30, warmup=10):
    """实测摄像头输出帧率（很多 UVC 相机上报值不准，实测更可靠）。"""
    for _ in range(warmup):
        cap.read()
    t0 = time.perf_counter()
    got = 0
    while got < frames_to_measure:
        ok, _ = cap.read()
        if ok:
            got += 1
        if time.perf_counter() - t0 > 3:  # 防止个别相机一直读不到帧卡死
            break
    dt = time.perf_counter() - t0
    fps = got / dt if dt > 0 else 30.0
    return max(1.0, min(fps, 120.0))


def open_writer(path, width, height, fps):
    """创建 VideoWriter，优先 H.264(avc1)，不行退回 mp4v。"""
    for fourcc in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), fps, (width, height))
        if writer.isOpened():
            return writer, fourcc
        writer.release()
    return None, None


def run_list():
    names, source = camera_names_for_index()
    if names:
        print(f"摄像头列表（来源：{source}）：")
        for i, name in enumerate(names):
            print(f"  [{i}] {name}")
    else:
        print("未能枚举到摄像头列表。")
        if sys.platform == "darwin":
            print("提示：加 --with pyobjc-framework-AVFoundation 可精确列出与索引对应的设备名。")

    print("\n正在逐个探测 OpenCV 可打开的设备，各存一张样张 cam_<i>.jpg（iPhone 连续互通相机可能较慢）…")
    for i in PROBE_RANGE:
        cap = open_camera(i)
        if not cap.isOpened():
            print(f"  [{i}] 打不开")
            cap.release()
            continue
        ok, frame = cap.read()
        if ok:
            h, w = frame.shape[:2]
            snap = f"cam_{i}.jpg"
            cv2.imwrite(snap, frame)
            print(f"  [{i}] 可用：{w}x{h}，上报帧率 {cap.get(cv2.CAP_PROP_FPS):.0f}，样张 {snap}")
        else:
            print(f"  [{i}] 能打开但读不到画面（可能未授权摄像头，或被其他应用占用）")
        cap.release()


def main():
    parser = argparse.ArgumentParser(description="用 USB 外接摄像头录视频")
    parser.add_argument("--list", action="store_true", help="列出摄像头后退出")
    parser.add_argument("--camera", type=int, help="摄像头索引（--list 可查看），默认自动选 USB 相机")
    parser.add_argument("--out", help="输出文件路径，默认 recording_时间戳.mp4")
    parser.add_argument("--width", type=int, default=1280, help="请求的画面宽度，默认 1280")
    parser.add_argument("--height", type=int, default=720, help="请求的画面高度，默认 720")
    parser.add_argument("--fps", type=float, help="强制指定写入帧率，默认自动实测")
    parser.add_argument("--max-seconds", type=float, help="最长录制秒数，默认一直录到手动停止")
    parser.add_argument("--no-preview", action="store_true", help="不弹预览窗口（配合 --max-seconds 使用）")
    args = parser.parse_args()

    if args.list:
        run_list()
        return

    index, name = resolve_camera(args.camera)
    cap = open_camera(index)
    if not cap.isOpened():
        print(f"[错误] 打不开摄像头 index={index}（{name}）。", file=sys.stderr)
        print("提示：macOS 首次使用需允许摄像头权限：", file=sys.stderr)
        print("  若刚才没弹授权窗，去 系统设置 → 隐私与安全性 → 摄像头，勾选你运行命令的终端应用。", file=sys.stderr)
        sys.exit(1)

    # 请求分辨率（相机不支持时会自动回落到自己的默认值）
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    ok, frame = cap.read()
    if not ok:
        print("[错误] 摄像头能打开但读不到画面，多半是权限未授予或设备被占用。", file=sys.stderr)
        cap.release()
        sys.exit(1)
    height, width = frame.shape[:2]

    fps = args.fps if args.fps else round(measure_fps(cap))
    out_path = Path(args.out or f"recording_{datetime.now():%Y%m%d_%H%M%S}.mp4")
    writer, fourcc = open_writer(str(out_path), width, height, fps)
    if writer is None:
        print("[错误] 无法创建视频文件，请检查输出路径。", file=sys.stderr)
        cap.release()
        sys.exit(1)

    preview = not args.no_preview
    if preview:
        try:
            cv2.namedWindow(WINDOW)
        except cv2.error:
            preview = False
            print("[提示] 无法创建预览窗口，自动切换为纯录制模式。")

    print(f"开始录制：{out_path}")
    print(f"  设备 [{index}] {name}，{width}x{height} @ {fps:.0f}fps，编码 {fourcc}")
    print("  预览窗口按 q / Esc 停止；也可以 Ctrl+C。")

    frames = 0
    fail_streak = 0
    start = time.perf_counter()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                fail_streak += 1
                if fail_streak > 60:  # 偶尔丢帧正常，连续失败才算真断流
                    print("[警告] 连续读取失败，提前结束录制。", file=sys.stderr)
                    break
                continue
            fail_streak = 0
            writer.write(frame)
            frames += 1
            elapsed = time.perf_counter() - start

            if preview:
                disp = frame.copy()
                cv2.circle(disp, (24, 26), 8, (0, 0, 255), -1)  # 红色 REC 圆点
                cv2.putText(disp, f"REC {elapsed:5.1f}s  {frames} frames", (42, 34),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                cv2.imshow(WINDOW, disp)
                if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):  # q / Esc
                    break

            if args.max_seconds and elapsed >= args.max_seconds:
                print(f"已到 --max-seconds {args.max_seconds:g}s，自动停止。")
                break
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止录制…")
    finally:
        writer.release()  # 必须 release 才能正确写完 mp4 尾部
        cap.release()
        cv2.destroyAllWindows()

    elapsed = time.perf_counter() - start
    if frames == 0:
        print("[错误] 一帧都没录到，未生成有效视频。", file=sys.stderr)
        sys.exit(1)
    size_mb = out_path.stat().st_size / 1e6
    print(f"已保存 {out_path}（{size_mb:.1f} MB），共 {frames} 帧 / {elapsed:.1f}s")
    actual_fps = frames / elapsed if elapsed > 0 else 0
    if abs(actual_fps - fps) / fps > 0.10:
        print(f"[提示] 实际帧率 {actual_fps:.1f} 与写入帧率 {fps:.0f} 偏差较大，"
              f"视频速度可能略有偏差；可加 --fps {actual_fps:.0f} 重录校正。")


if __name__ == "__main__":
    main()
