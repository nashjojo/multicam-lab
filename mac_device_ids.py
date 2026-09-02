#!/usr/bin/env python3
"""macOS 设备身份标识：把摄像头的视频端与它自带的麦克风精确对上。

**为什么不能只按名字配**：同型号的两台摄像头，视频端与音频端的名字都完全
一样，按名字配对可能交叉 —— 而两路都有数据、标签也对，属于静默错配，事后
无从分辨。连单台设备都已经有同名歧义：实测 1080P USB Camera 暴露了两个都叫
"1080P USB Camera-Audio" 的 CoreAudio 设备，一个是麦、一个是喇叭（该机型确实
带喇叭，播放实测确认过）。

实测出来的配对链（每一环都在本机验证过，见 probe_devices.py）：

    视频端 AVCaptureDevice.uniqueID  0x21420002bdf0296
      └ 高 32 位 = USB locationID     0x02142000
          └ (ioreg) USB 序列号        DC474C08_P090101_SN0002
              └ 含该序列号的 CoreAudio DeviceUID
                AppleUSBAudioEngine:…:DC474C08_P090101_SN0002:3

音频端标识里嵌的是**序列号**而不是 locationID，所以必须借 ioreg 从 location
转一手，没法一步对上。

仅 macOS 有效。任何一环拿不到就返回空结果，调用方据此回退到名字匹配。
"""

import ctypes
import plistlib
import re
import struct
import subprocess
import sys

# CoreAudio 的属性选择符与作用域都是四字符码。**权威值要用 pyobjc 的
# CoreAudio 模块核对**（如 kAudioDevicePropertyStreamConfiguration）：记错
# 一个字母，HAL 只回 'who?'（kAudioHardwareUnknownPropertyError），既不报
# 参数错也不崩，极难查 —— 实测把 'slay' 写成 'slty' 就是这个症状。
_SYSTEM_OBJECT = 1        # kAudioObjectSystemObject
_SEL_DEVICES = "dev#"     # kAudioHardwarePropertyDevices
_SEL_NAME = "name"        # kAudioObjectPropertyName
_SEL_UID = "uid "         # kAudioDevicePropertyDeviceUID（注意末尾空格）
_SEL_STREAM_CFG = "slay"  # kAudioDevicePropertyStreamConfiguration
_SCOPE_GLOBAL = "glob"
_SCOPE_INPUT = "inpt"
_SCOPE_OUTPUT = "outp"


def usb_location_of_uid(uid: str) -> int | None:
    """从 AVCaptureDevice.uniqueID 取 USB locationID（高 32 位）。

    UVC 摄像头的 uniqueID 是十六进制串，如 ``0x21420002bdf0296``：高 32 位是
    locationID（``0x02142000``，即插在哪个物理口，换口即变），低 32 位是
    vendor/product id。**长度不定**（locationID 高位的前导零会被省掉，实测
    只有 15 位），不能按定长 16 位判断。内建摄像头与连续互通设备是 UUID
    形态，没有「插在哪个口」的概念，返回 None。
    """
    if not uid:
        return None
    s = uid[2:] if uid.lower().startswith("0x") else uid
    # 少于 9 位说明高 32 位是 0，即没有 location 段
    if 9 <= len(s) <= 16 and re.fullmatch(r"[0-9a-fA-F]+", s):
        return int(s, 16) >> 32
    return None


def usb_serial_by_location() -> dict:
    """走 ioreg 拿 USB 树，返回 ``{locationID: 序列号}``；失败返回空表。

    用 ``ioreg -a``（XML plist）而不是解析默认的缩进文本：文本格式下属性的
    打印顺序没有保证，靠「产品名开头、后面跟 locationID」的行序假设去凑很
    脆弱，实测就漏掉了几个节点的 locationID。
    """
    if sys.platform != "darwin":
        return {}
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-a"],
                             capture_output=True, timeout=15).stdout
        root = plistlib.loads(out)
    except Exception:
        return {}
    found: dict = {}

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        loc, sn = node.get("locationID"), node.get("USB Serial Number")
        if isinstance(loc, int) and isinstance(sn, str) and sn:
            found[loc] = sn
        walk(node.get("IORegistryEntryChildren") or [])

    walk(root)
    return found


def _fourcc(code: str) -> int:
    """四字符码 → u32，如 'uid ' → 0x75696420。"""
    return (ord(code[0]) << 24) | (ord(code[1]) << 16) \
        | (ord(code[2]) << 8) | ord(code[3])


class _PropAddr(ctypes.Structure):
    """AudioObjectPropertyAddress：选择符 + 作用域 + 元素。"""

    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


def _total_channels(raw: bytes | None) -> int:
    """数 StreamConfiguration 里的总通道数。

    返回形态是 AudioBufferList：``{mNumberBuffers:u32, 4 字节对齐填充,
    mBuffers[]: AudioBuffer{mNumberChannels:u32, mDataByteSize:u32,
    mData:ptr}（每条 16 字节）}``，总通道数是各 buffer 通道数之和。
    """
    if not raw or len(raw) < 8:
        return 0
    count = struct.unpack_from("<I", raw, 0)[0]
    total = 0
    for i in range(count):
        off = 8 + i * 16
        if off + 4 > len(raw):
            break
        total += struct.unpack_from("<I", raw, off)[0]
    return total


def coreaudio_devices() -> list:
    """枚举 CoreAudio HAL 设备，返回 ``[{id, name, uid, in, out}]``。

    顺序就是 HAL 的返回顺序（实测与 PortAudio 的 Core Audio 宿主设备顺序
    一致，:func:`portaudio_index_of_uid` 靠这点换算设备号）。

    sounddevice 不暴露设备的 CoreAudio UID，只能 ctypes 直调 CoreAudio。UID
    是 HAL 层的稳定标识（跨进程、跨重启不变）；而 ``id`` 只是当次 HAL 会话里
    的句柄，设备插拔就会重新分配，**不能缓存或跨进程传递**。
    """
    if sys.platform != "darwin":
        return []
    try:
        ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/"
            "CoreFoundation")
    except Exception:
        return []
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_ubyte
    utf8 = 0x08000100  # kCFStringEncodingUTF8

    def get(obj: int, sel: str, scope: str = _SCOPE_GLOBAL) -> bytes | None:
        """取属性数据，直接给足缓冲。

        **不先用 AudioObjectGetPropertyDataSize 问长度**：实测它对含非 ASCII
        的名字返回的长度偏小（"MacBook Pro麦克风" 只报 15），随后的取值会把
        数据截断成 "MacBook Pro麦"。
        """
        addr = _PropAddr(_fourcc(sel), _fourcc(scope), 0)
        size = ctypes.c_uint32(4096)
        buf = ctypes.create_string_buffer(4096)
        if ca.AudioObjectGetPropertyData(
                obj, ctypes.byref(addr), 0, None,
                ctypes.byref(size), buf) != 0:
            return None
        return buf.raw[:size.value]

    def as_inline_str(raw: bytes | None) -> str | None:
        """内联 C 字符串形态（实测 kAudioObjectPropertyName 是这个形态：
        取回的就是 UTF-8 字节 + NUL，不是指针）。"""
        if not raw:
            return None
        s = raw.split(b"\x00", 1)[0]
        return s.decode("utf-8", "replace") if s else None

    def as_cfstring(raw: bytes | None) -> str | None:
        """CFStringRef 指针形态（实测 kAudioDevicePropertyDeviceUID 是这个
        形态）。先试直取内部指针，拿不到再拷贝转换。"""
        if not raw or len(raw) != 8:
            return None
        ref = struct.unpack("<Q", raw)[0]
        if not ref:
            return None
        direct = cf.CFStringGetCStringPtr(ctypes.c_void_p(ref), utf8)
        if direct:
            return direct.decode("utf-8", "replace")
        n = cf.CFStringGetLength(ctypes.c_void_p(ref))
        if n <= 0 or n > 4096:
            return None
        buf = ctypes.create_string_buffer(n * 4 + 8)
        if cf.CFStringGetCString(ctypes.c_void_p(ref), buf, len(buf), utf8):
            return buf.value.decode("utf-8", "replace")
        return None

    raw = get(_SYSTEM_OBJECT, _SEL_DEVICES)
    if not raw or len(raw) < 4:
        return []
    out: list = []
    for did in struct.unpack(f"<{len(raw) // 4}I", raw[:len(raw) // 4 * 4]):
        out.append({
            "id": did,
            "name": as_inline_str(get(did, _SEL_NAME)),
            "uid": as_cfstring(get(did, _SEL_UID)),
            "in": _total_channels(get(did, _SEL_STREAM_CFG, _SCOPE_INPUT)),
            "out": _total_channels(get(did, _SEL_STREAM_CFG, _SCOPE_OUTPUT)),
        })
    return out


def portaudio_core_audio_devices() -> list:
    """PortAudio 的 Core Audio 宿主设备，返回 ``[{pa_idx, name, in, out}]``。

    顺序即 PortAudio 的枚举顺序，用来和 :func:`coreaudio_devices` 逐位对齐。
    """
    try:
        import sounddevice as sd
    except Exception:
        return []
    try:
        apis = sd.query_hostapis()
    except Exception:
        return []
    for api in apis:
        if "core audio" not in str(api.get("name", "")).lower():
            continue
        devs: list = []
        for idx in api.get("devices", []):
            if idx < 0:
                continue
            try:
                d = sd.query_devices(idx)
            except Exception:
                continue
            devs.append({"pa_idx": idx, "name": d["name"],
                         "in": d["max_input_channels"],
                         "out": d["max_output_channels"]})
        return devs
    return []


def _hal_to_portaudio(hal: list, pa: list) -> dict:
    """建立 ``{HAL 设备 id: PortAudio 设备号}`` 映射；对不上返回空表。

    实测 HAL 顺序与 PortAudio 的 Core Audio 宿主顺序一致，故按位对齐。但
    **必须逐位核对名字与通道数**：万一哪天 PortAudio 改了过滤或排序规则，
    错位的映射会把麦配到别的设备上（静默错配），宁可返回空表退回名字匹配。
    """
    if not hal or not pa:
        return {}
    seqs = [hal]
    # PortAudio 是否会滤掉零通道设备，本机没有这种设备、无从判定，两种都试。
    if len(hal) != len(pa):
        seqs.append([d for d in hal if d["in"] or d["out"]])
    for cand in seqs:
        if len(cand) != len(pa):
            continue
        if all(c["name"] == p["name"] and c["in"] == p["in"]
               and c["out"] == p["out"] for c, p in zip(cand, pa)):
            return {c["id"]: p["pa_idx"] for c, p in zip(cand, pa)}
    return {}


def mic_by_camera_uid(cam_uids: list) -> dict:
    """按 uniqueID 给每台摄像头找它自带的麦克风。

    入参是摄像头的 AVCaptureDevice uniqueID 列表，返回
    ``{cam_uid: (PortAudio 设备号, 设备名)}``；配不上的摄像头**不出现**在
    返回里，调用方据此回退到名字匹配。

    以下情形一律视为「配不上」而不猜：

    * 序列号被多台在录摄像头共用（同型号劣质固件可能不给唯一序列号）——
      分不出谁是谁，猜错就是静默错配；
    * 同一序列号下有多个可输入的音频设备 —— 无从判断哪个才是那支麦。
    """
    uids = [u for u in cam_uids if u]
    if not uids:
        return {}
    loc_of = {u: usb_location_of_uid(u) for u in uids}
    if not any(v is not None for v in loc_of.values()):
        return {}  # 全是内建/连续互通设备，无需走 USB 链路
    serial_of_loc = usb_serial_by_location()
    if not serial_of_loc:
        return {}
    serial_of: dict = {}
    for uid, loc in loc_of.items():
        sn = serial_of_loc.get(loc) if loc is not None else None
        if sn:
            serial_of[uid] = sn
    if not serial_of:
        return {}
    # 序列号撞车的摄像头全部排除：宁可退回名字匹配，也不猜哪路是哪路
    shared = {sn for sn in serial_of.values()
              if list(serial_of.values()).count(sn) > 1}
    hal = coreaudio_devices()
    pa_of_hal = _hal_to_portaudio(hal, portaudio_core_audio_devices())
    if not pa_of_hal:
        return {}
    out: dict = {}
    for uid, sn in serial_of.items():
        if sn in shared:
            continue
        # 同一台设备可能暴露多个音频接口（实测 1080P 的 :3 是麦、:4 是喇叭），
        # 只有能输入的才是麦。
        mics = [d for d in hal
                if d["uid"] and sn in d["uid"] and d["in"] > 0
                and d["id"] in pa_of_hal]
        if len(mics) != 1:
            continue
        mic = mics[0]
        out[uid] = (pa_of_hal[mic["id"]], mic["name"] or "")
    return out
