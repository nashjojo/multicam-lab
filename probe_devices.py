#!/usr/bin/env python3
"""设备标识体检：把「摄像头 ↔ 自带麦克风」的配对链逐环摆出来看。

录制侧 multi_cam_recorder.resolve_cam_mics() 优先按 **USB 身份**配麦（配不上
才退回名字匹配）。本脚本调用的就是它依赖的同一批函数（mac_device_ids），
所以这里看到的结论与录制时的实际行为一致，可用来排查「某路没有音频」
「音频疑似配串」这类问题。

打印四套命名体系的标识与它们的对应关系：

  1. AVFoundation 视频设备（cv2.VideoCapture 索引的来源） → uniqueID
  2. AVFoundation 音频设备（AVCaptureDevice 'soun'）        → uniqueID
  3. CoreAudio HAL 设备（PortAudio / sounddevice 的来源）    → DeviceUID
  4. ioreg 的 USB 树                                        → locationID / 序列号

配对链（每一环都在本机验证过）：视频 uniqueID 的高 32 位是 USB locationID
→ 经 ioreg 换到该口设备的序列号 → 找 UID 里含这个序列号的音频设备。音频端
标识形如 ``AppleUSBAudioEngine:<厂商>:<产品>:<序列号>:<接口号>``，嵌的是
**序列号**而不是 locationID，所以中间必须借 ioreg 转一手。

**同型号两台摄像头的自查重点**：看第 5 节两台的序列号是否不同。相同就说明
固件没给唯一序列号，配对链会断（第 6 节会显示配不上），此时只能靠声学校验
区分。

用法：
    uv run probe_devices.py
"""

import sys

import mac_device_ids as ids


def avfoundation_audio_devices() -> list:
    """枚举音频类 AVCaptureDevice，返回 [(名字, uniqueID)]。

    纯诊断用：它的 uniqueID 与 CoreAudio 的 DeviceUID 是同一个字符串，把两边
    都打出来可以互相印证（录制侧只用 CoreAudio 那条路）。
    """
    if sys.platform != "darwin":
        return []
    try:
        from AVFoundation import AVCaptureDevice
    except ImportError:
        return []
    try:
        devices = list(AVCaptureDevice.devicesWithMediaType_("soun"))
    except Exception:
        return []
    devices.sort(key=lambda d: str(d.uniqueID()))
    return [(str(d.localizedName()), str(d.uniqueID())) for d in devices]


def main():
    from multi_cam_recorder import pick_usb_auto, resolve_cam_mics
    from usb_cam_recorder import camera_entries_for_index

    vids, source = camera_entries_for_index()
    print("== 1. AVFoundation 视频设备（cv2 索引来源）==")
    print(f"  来源：{source}")
    for i, (name, uid) in enumerate(vids):
        loc = ids.usb_location_of_uid(uid or "")
        tail = f"  USB location=0x{loc:08x}" if loc is not None else "  （非 USB）"
        print(f"  cv2[{i}] {name!r}  uniqueID={uid}{tail}")

    auds = avfoundation_audio_devices()
    print("\n== 2. AVFoundation 音频设备（AVCaptureDevice 'soun'）==")
    for name, uid in auds:
        print(f"  {name!r}  uniqueID={uid}")

    hals = ids.coreaudio_devices()
    print("\n== 3. CoreAudio HAL 设备（sounddevice 的来源，HAL 顺序）==")
    for d in hals:
        print(f"  id={d['id']:<4} {d['name']!r}  ({d['in']} in / {d['out']} out)"
              f"  uid={d['uid']!r}")

    pas = ids.portaudio_core_audio_devices()
    print("\n== 4. sounddevice / PortAudio（Core Audio 宿主顺序）==")
    for d in pas:
        print(f"  pa[{d['pa_idx']:<2}] {d['name']!r}"
              f"  ({d['in']} in / {d['out']} out)")

    print("\n== 5. ioreg USB 树：locationID → 序列号 ==")
    serials = ids.usb_serial_by_location()
    if not serials:
        print("  （取不到；配对会退回名字匹配）")
    for loc, sn in sorted(serials.items()):
        print(f"  0x{loc:08x}  sn={sn!r}")

    print("\n== 6. 配对链：视频 uniqueID → location → 序列号 → 音频设备 ==")
    hits = ids.mic_by_camera_uid([u for _, u in vids if u])
    for i, (name, uid) in enumerate(vids):
        loc = ids.usb_location_of_uid(uid or "")
        if loc is None:
            print(f"  cv2[{i}] {name!r}：非 USB 设备，只能按名字配")
            continue
        sn = serials.get(loc)
        hit = hits.get(uid)
        if hit:
            print(f"  cv2[{i}] {name!r} → pa[{hit[0]}] {hit[1]!r}"
                  f"（sn={sn!r}）")
        else:
            why = "ioreg 里查不到该口的序列号" if not sn else \
                "序列号与在录机位撞车，或该序列号下可输入设备不唯一"
            print(f"  cv2[{i}] {name!r}：配不上 —— {why}")

    print("\n== 7. HAL ↔ PortAudio 顺序对齐检查 ==")
    mapping = ids._hal_to_portaudio(hals, pas)
    if mapping:
        print(f"  一致，可换算设备号（{len(mapping)} 个设备）")
    else:
        print("  对不上！UID 配对会整体退回名字匹配（顺序或名字/通道数不符）")

    print("\n== 8. 录制时的实际配麦结果（--usb 自动选择的机位）==")
    usb_indices = pick_usb_auto()
    names = [n for n, _ in vids]
    uids = [u for _, u in vids]
    mics = resolve_cam_mics(usb_indices, names, uids)
    for cam in usb_indices:
        got = mics.get(cam)
        cam_name = names[cam] if cam < len(names) else "?"
        if got is None:
            print(f"  usb[{cam}] {cam_name!r}：无可用麦克风")
            continue
        dev, dev_name, how = got
        if how == "name":
            # 非 USB 设备本就只能按名字配，不算降级；USB 设备退到名字才有配串风险
            is_usb = ids.usb_location_of_uid(
                uids[cam] or "" if cam < len(uids) else "") is not None
            note = ("按名字匹配 ⚠ USB 身份没配上，同型号多机位下有配串风险"
                    if is_usb else "按名字匹配（非 USB 设备，只能如此）")
        else:
            note = {"uid": "USB 身份精确匹配",
                    "default": "内建机位兜底到系统默认输入"}.get(how, how)
        print(f"  usb[{cam}] {cam_name!r} → pa[{dev}] {dev_name!r}  [{note}]")


if __name__ == "__main__":
    main()
