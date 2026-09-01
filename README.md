# multicam-lab — 多机位同步录制 / 延迟标定 / 回放查看

一套自包含的数据集制作工具链：**同步录多路视频 + 音频 → 校验 → 在浏览器里按同一
时间轴回放比对**。独立仓库，有自己的 `pyproject.toml` 与 `.venv`。

各机位通常**分布在不同地点、视野不重叠**（所以是 multi-camera 而非 multi-view —— 这里
没有重叠视野，也就没有对极几何可用）。它要服务的目标是**跨镜头的物品位置追踪**：手机在
cam1 被放进包里、包被拿到 cam2 取出放进袋子、袋子被带到 cam3，然后问「手机最终在哪里」。
这类题目的真值完全依赖**跨镜头时间轴可信** —— 包在 cam1 消失、在 cam2 出现，要能断定是
同一个包、中间隔了多久。所有源共用主机同一个墙钟正是为此（实测最大起点差 20ms）。

**默认只用本机摄像头**，不接米家也能走完整流程 —— 自动挑选会把外置 USB 与内置摄像头
一并带上，只跳过 iPhone / iPad 连续互通（它会唤醒手机，而且手机在不在身边会改变整张
索引表，让写死的 `--usb N` 指错机位）；要用它就显式 `--usb <索引>`。

米家摄像头是可选源：显式加 `--miot` 才会去连 miloco 后端（`http://127.0.0.1:1810`），
后端不可达时只是跳过米家、不影响录制。

## 快速上手

```bash
cd multicam-lab
uv sync                  # 首次建环境（只需一次）
./rec_session.sh         # 起录制会话
```

`rec_session.sh` 走的是 `uv run --frozen --offline`：环境既然已经建好，开录前就不该再
联网解析依赖。若你的 shell 里配了只能内网访问的 PyPI 镜像（`UV_DEFAULT_INDEX` /
`UV_INDEX_URL` 之类），裸 `uv run rec_session.py` 会在启动前卡几十秒然后失败 ——
日常一律用这个脚本，别绕过它。

会话里的按键：

| 键 | 作用 |
|---|---|
| `space` | 开始录制 / 录制中按下则提前结束（保留已录内容）/ 结束后再按录下一段 |
| `v` | 打印当前最新一段的预览地址 |
| `q` | 退出（会等在录的那段收尾） |

每段录完会在后台解码校验并打一行 `✓`/`✗`（帧数、时长、USB 速度比自洽性、音频丢包），
校验与预览一律走后台线程 —— 拍摄现场最怕的是「想录的瞬间工具在忙」。

常用参数：`--seconds` 单段上限、`--usb 0 --usb 1` 手选机位、`--miot` 带上米家、
`--no-audio` / `--no-verify` / `--no-serve` / `--no-voice`。

## 产物

一次录制 = 一个自包含文件夹，可整体搬迁归档而不断链：

```
sync_clips/20260901_143022/
  manifest.json     各路起点偏移、帧率、音轨与对齐用的 ffmpeg 提示
  usb0.mp4  usb1.mp4  ...
  usb0.wav  ...
```

对齐锚点是「首帧到达主机的墙钟时刻」，所有源共用本机同一个时钟，因此不含跨机时钟
偏差。清单里的 `start_offset_ms` 就是各路相对最早起点的差值。

## 工具清单

| 脚本 | 用途 |
|---|---|
| `rec_session.py` | **日常入口**：一键起服务、按键反复录段、后台校验 + 预览 |
| `multi_cam_recorder.py` | 录制本体。`--list` 看有哪些源可用；单跑也可以 |
| `usb_cam_recorder.py` | 单路 USB 录制 / 列摄像头并存样张，排查「哪个索引是哪个机位」 |
| `miot_cam_recorder.py` | 单跑米家录制（纯标准库，需 miloco 后端） |
| `serve_viewer.py` | 起回放服务；`sync_viewer.html` 是页面本体 |
| `beep_calibrate.py` | 蜂鸣标定音频链路延迟 |
| `flash_calibrate.py` | 闪光标定视频链路延迟 |

## 回放查看

录制会话默认自带常驻预览服务，直接用它打印的 URL 即可。单独看历史录制：

```bash
uv run serve_viewer.py          # 自动挑最新一份清单并打印 URL
uv run serve_viewer.py --dir sync_clips --port 8000
```

**别用 `python3 -m http.server`** —— 它对 `Range` 请求返回 200 + 全量而非 206 分片，
Chrome 于是把视频判定为不可 seek：能播，但拖动进度条画面完全不动。`serve_viewer.py`
存在的唯一理由就是补上 206。（不想起服务也行：直接用浏览器打开 `sync_viewer.html`，
把 `manifest.json` 和 mp4 一起拖进页面 —— 那条路径走 blob URL，天然可 seek。）

## 延迟标定

各路「画面/声音到达主机」的延迟不同（USB 本机直连几十毫秒，米家要走相机编码 →
网络传输 → 主机解码，实测 0.5–1s）。要把这部分补偿掉就得先量出来：

```bash
uv run beep_calibrate.py --list-mics       # 先确认麦克风
uv run beep_calibrate.py --usb-mic 2       # 本机播三声蜂鸣，各路听，量音频链路
uv run flash_calibrate.py                  # 全屏黑白闪烁，量视频链路
uv run flash_calibrate.py --miot           # 连米家一起标（需后端可达）
```

两者互为交叉验证：全程只用主机自己的时钟打点，**不读相机 RTC**，所以相机时钟误差
不参与 —— 这是它优于「读画面水印」的地方（水印法测出的是「延迟 + 相机时钟误差」，
两者无法分离）。

`flash_calibrate.py` 用 tkinter 全屏闪烁 —— uv 托管的 Python 自带 Tk（本机实测
Tk 9.0，`uv sync` 后即可用）；只有换成不带 Tk 的解释器时才需自行补装。

## 平台与边界

macOS 专用：用到 `system_profiler SPCameraDataType` 枚举摄像头、AVFoundation 兜底、
`say -v Tingting` 播语音提示。

首次录制会请求摄像头 / 麦克风权限，但 macOS 把权限记在**启动终端的那个 app** 上
（Terminal、iTerm、IDE 内置终端…），不是脚本、也不是 Python。若「系统设置 → 隐私与
安全性 → 摄像头」里没有它，系统会直接拒绝且**不弹框**，表现为 OpenCV 报
`not authorized to capture video` 后每路都「准备失败」。该面板不能手动添加应用，换一个
已授权的终端 app 跑即可。

与 miloco 主项目只有网络边界（REST/WS），后端地址与 token 读
`~/.openclaw/miloco/config.json`（可用 `--url` / `--token` 或 `MILOCO_URL` /
`MILOCO_TOKEN` 覆盖），不读仓库里的任何配置。

对齐锚点只用主机自己的墙钟：米家侧取后端响应头 `X-Clip-First-Frame-Unix-Ms`（首帧到达
后端的时刻），USB 侧取首个保留帧的 `time.time()`，两者同源。各路先在 `threading.Barrier`
上会合再统一放行，避免预热耗时不同导致的起点错位。**不读相机 RTC** —— 相机时钟误差因此
完全不参与，这是它优于「读画面水印」的地方（水印法测出的是「延迟 + 相机时钟误差」，两者
无法分离）。
