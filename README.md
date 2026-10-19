# VK B240W 屏幕驱动

这是一个适用于 Windows 的 Python 程序，可在 VK B240W 的 320×240 屏幕上显示静态图片、文字、动态 GIF 和视频。

## 抓包内容

提供的 `t.pcap` 抓包将显示设备识别为 `VID_345F&PID_9132`，接口 3（`MI_03`），Bulk OUT 端点为 `0x04`。每帧抓取数据为 153,616 字节：

- 8 字节帧前缀：`FF 00 00 00 00 14 00 F0`
- 320×240 UYVY（YUV 4:2:2）像素数据：153,600 字节
- 8 字节帧后缀：`FF C0 00 00 00 00 00 00`

抓包显示，每帧依次通过 65,536、65,536 和 22,544 字节的写入传输，随后发送一次零长度 Bulk OUT 写入。抓包中第一帧之前有一次零长度写入，这可能是抓包开始前上一帧被截断后留下的结束标记；之后每组三次帧数据写入后都有一个结束标记。抓包使用的 65,535 字节数据包上限截断了前两次传输的末尾。UYVY 字节顺序是根据像素字节模式和帧大小推断的；帧封装格式和端点则是直接从抓包中观察到的。程序会先异步提交相同大小的三段数据，等待它们传输完成后，再发送抓包中观察到的帧结束标记。

## USB 驱动

程序使用 PyUSB 和 `libusb-win32` 后端传输视频，并通过 `hidapi` 使用设备的 HID 控制接口。对本设备进行只读识别后，得到的芯片签名为 `B7 16 0A`（MS912C）、SDRAM 类型为 2、视频端口为 5、HPD 为 1。Windows 将接口 3 显示为 `MS USB Display`，服务为 `libusb0`。运行脚本前，请关闭 MythCool 或其他正在使用该屏幕的软件；同一时间只能有一个程序占用此 USB 接口。

如果 Windows 尚未为接口 3（`MI_03`）安装兼容的 `libusb0` 驱动，请先安装匹配的驱动。不要更改复合设备父设备、HID 接口 0 或 USB 音频接口 1 的驱动。更改 `MI_03` 的驱动绑定后，厂商软件可能无法再使用该接口，除非恢复原来的驱动。HID 接口通过 Windows HID 打开，无需更改其驱动。

## 安装与运行

建议使用 Python 3.10 或更高版本。

```powershell
py -m pip install -r requirements.txt
py .\vk_b240w.py image .\picture.png
py .\vk_b240w.py image .\picture.png --fps 2
py .\vk_b240w.py text "Hello VK B240W"
py .\vk_b240w.py text "水冷温度 42°C" --font-size 28 --color white --background "#102030" --font .\font.ttf
py .\vk_b240w.py image .\picture.png --once
py .\vk_b240w.py play .\animation.gif
py .\vk_b240w.py play .\video.mp4
py .\vk_b240w.py play .\video.mp4 --max-fps 15
py .\vk_b240w.py play .\video.mp4 --cache-memory
py .\vk_b240w.py play .\video.mp4 --max-fps 10 --cache-memory

py .\vk_b240w.py play .\video.mp4 --max-fps 10 --cache-disk
py .\vk_b240w.py play .\video.mp4 --cache-disk --flush-cache
py .\vk_b240w.py play .\video.mp4 --cache-disk --startup-attempts 3 --restart-after 12
py .\vk_b240w.py play .\video.mp4 --cache-disk --hide
py .\vk_b240w.py play .\video.mp4 --cache-disk --hide --no-log
py .\vk_b240w.py test-pattern
```

### 使用 uv 打包为无控制台单文件 exe

无控制台 exe 的效果相当于使用 `pythonw.exe` 运行脚本，不需要额外打包或分发 `pythonw.exe`。安装好 `uv` 后，在项目目录执行：

```powershell
.\build.ps1
```

脚本会创建 `.venv`、安装依赖并构建单文件程序。生成的 `dist\vk_b240w_no_console.exe` 不会显示控制台窗口；默认运行日志仍写入 `%LOCALAPPDATA%\VKCache\logs`。单文件 exe 内含 FFmpeg，无需单独分发。命令行和错误信息在无控制台运行时不会显示，可通过日志查看运行状态。

### 命令参数

| 命令 | 参数 | 说明 |
|---|---|---|
| `image <file>` | `--fps N` | 每秒以 N 帧刷新静态图片；默认值为 5。 |
| `image <file>` | `--once` | 只发送一帧后退出，不持续刷新。 |
| `text <text>` | `--color COLOR` | 文字颜色；默认值为 `white`。 |
| `text <text>` | `--background COLOR` | 背景颜色；默认值为 `black`。 |
| `text <text>` | `--font-size N` | 字体大小（像素）；默认值为 24。 |
| `text <text>` | `--font FILE` | 可选的 `.ttf`/`.ttc` 字体文件。 |
| `text <text>` | `--fps N` | 每秒以 N 帧刷新文字；默认值为 5。 |
| `text <text>` | `--once` | 只发送一帧后退出，不持续刷新。 |
| `play <file>` | `--max-fps N` | 可选的最大帧率限制。不设置时遵循 GIF 帧延迟或视频帧率。 |
| `play <file>` | `--cache-memory` | 将动画解码并编码一次后缓存到内存；循环播放前 FFmpeg 会退出。会显示预计和实际帧缓存大小。 |
| `play <file>` | `--cache-disk` | 复用或生成一个磁盘缓存；缓存生成后 FFmpeg 会退出。缓存位于 `%LOCALAPPDATA%\VKCache`，文件名为 `<视频名>_<帧率>fps.vkcache`。 |
| `play <file>` | `--flush-cache` | 强制重新生成磁盘缓存；需与 `--cache-disk` 一起使用。 |
| 所有命令 | `--startup-attempts N` | 对 MS912x 命令超时进行启动重试的最大尝试次数，包含首次尝试；默认 3 次。其他初始化错误不会重试。 |
| 所有命令 | `--restart-after HOURS` | 连续运行指定小时数后重新启动 Python 进程；默认 12 小时。设为 `0` 可禁用。 |
| 所有命令 | `--hide` | 在 Windows 上完全隐藏控制台窗口；程序不会输出控制台信息，仅在任务管理器中显示进程。 |
| 所有命令 | `--no-log` | 不生成运行日志；默认会在 `%LOCALAPPDATA%\VKCache\logs` 中生成 JSONL 日志。 |
| `test-pattern` | `--fps N` | 每秒以 N 帧刷新校准图；默认值为 5。 |
| `test-pattern` | `--once` | 发送一帧校准图后退出。 |

所有命令都支持 `-h`/`--help`；使用 `py .\vk_b240w.py <command> --help` 查看指定命令的选项。为保持控制器处于活动状态，静态图片和文字默认以 5 FPS 持续刷新；可通过 `--fps` 更改刷新频率。

`image` 会在开始传输前只解码、缩放并编码一次图片，之后重复发送同一份 153,616 字节帧数据；发送所需的三个 USB 数据块也只创建一次并复用，不会在每次刷新时重新复制整帧。图片文件的压缩大小不等于解码后的像素内存大小；进程工作集还包括 Python、Pillow 和 NumPy 等运行库，且内存分配器不一定会在临时处理结束后立即将已释放内存归还操作系统。

`play` 会循环播放动态 GIF 和 FFmpeg 支持的视频文件（包括 MP4、MOV、MKV 和 AVI），并将每帧适配到屏幕尺寸。默认按照源帧时间播放。可通过 `--max-fps` 限制最高帧率；如果设置了该选项，FFmpeg 会在将视频帧交给 Python 前丢弃多余帧，使播放尽量接近实时。GIF 在限帧时也会遵循帧延迟。按 `Ctrl+C` 停止播放。

`--cache-memory` 会读取一次动画，将帧转换为设备所需的 USB 编码格式，然后关闭 FFmpeg，并从内存循环播放编码后的帧。程序会在解码前显示预计大小，并在缓存完成后显示实际帧数据大小。一帧完整的 320×240 图像占用 153,616 字节，因此一分钟、30 FPS 的视频需要约 276 MB 帧数据（另有少量 Python 对象开销）；使用 `--max-fps 10` 时约为 92 MB。提供的 73.8 秒、320×240、25 FPS MP4，在不限制帧率时预计占用约 283 MB，使用 `--max-fps 10` 时约为 113 MB。如果担心内存占用，请不要使用 `--cache-memory`，或调低 `--max-fps`。缓存可以避免循环播放时重复启动 FFmpeg、解码和转换像素，但 USB 传输仍需要时间，因此无法让播放速度超过设备的传输/触发限制。在不受 USB 速度限制的基准测试中，提供的 MP4 使用 `--max-fps 10` 未缓存播放一遍约需 5.4 秒；从预编码内存缓存播放一遍所需的处理时间几乎可以忽略。生成缓存本身约需 5.1 秒，因此缓存主要能降低重复循环时的 CPU 占用，不会加快首次播放或屏幕刷新。

`--cache-disk` 的工作方式类似，但会将编码后的帧保存到磁盘，而不是把整个动画保留在内存中。缓存文件直接保存在 `%LOCALAPPDATA%\VKCache`，文件名包含源文件名和实际播放帧率，例如 `video_25fps.vkcache`；如果设置了 `--max-fps`，则采用源帧率和限制值中较低的那个。该目录中只保留一个 `.vkcache` 文件。缓存头部也会记录源文件名，因此源文件名和帧率都相同时会复用缓存。如果视频内容改变但文件名没有改变，请使用 `--flush-cache` 重新生成。`--cache-memory` 和 `--cache-disk` 不能同时使用；`--flush-cache` 必须与 `--cache-disk` 一起使用。

## 关于 cache

视频播放有实时解码、缓存到硬盘和缓存到内存三种方式：

| 方式 | 使用方法 | 优点 | 缺点 |
|---|---|---|---|
| FFmpeg 实时解码 | 默认（不加缓存参数） | 无需预先生成缓存，启动快，不额外占用大量磁盘或内存。 | 每次循环都要重新解码和转换帧，CPU 开销较高。 |
| 缓存到硬盘 | `--cache-disk` | 可复用已生成的缓存；相比内存缓存占用更少 RAM，适合较长视频。 | 首次播放需生成缓存，会占用磁盘空间并产生读写开销。 |
| 缓存到内存 | `--cache-memory` | 循环播放时无需再次解码，CPU 开销低，读取缓存也快。 | 需要将全部编码帧放入内存，长视频或高帧率可能占用大量 RAM；开始播放前需完成缓存。 |

三种方式都受 USB 传输和屏幕刷新速度限制；缓存不能提高屏幕的最高刷新速度。内存有限时优先使用实时解码或磁盘缓存，重复播放且内存充足时可使用内存缓存。

程序默认会在 `%LOCALAPPDATA%\VKCache\logs` 为每次运行创建一个 JSON Lines 日志，并在控制台显示日志文件路径。日志记录命令类型、输入文件名及帧率/缓存/重试/定时重启等设置、缓存是否复用或生成、设备初始化与启动重试、播放启动/结束、定时重启、正常退出和错误详情（包括 Python traceback）；不会记录文字命令的正文。日志不会周期性采样内存，也不会逐帧写入，以避免不必要的性能开销。使用 `--no-log` 可完全禁用日志。启用日志时还会注册轻量级崩溃追踪；正常退出不会留下空的 `.crash.log`，若 Python 遇到致命崩溃，则可能生成同名 `.crash.log`，记录故障线程和栈信息。`--hide` 模式下日志仍照常写入文件，但不会显示控制台输出；如需定位问题，请将对应 `.jsonl` 和（若存在）`.crash.log` 文件发回。

程序启动时，如果 MS912x 命令因为控制器尚未就绪而超时，会在清理 USB/HID 资源后等待 1 秒并重新初始化，最多尝试 `--startup-attempts` 指定的次数。其他错误（例如设备未连接、驱动缺失）会立即报错，不做无意义的重试。长时间连续运行时，达到 `--restart-after` 小时后，程序会先关闭设备连接和运行日志，再用原命令行重新启动 Python 进程，以重置 Python 和本机库的进程内存；默认 12 小时，设置为 `0` 可关闭。此计时从屏幕初始化完成、开始传输时启动，不包含缓存构建或启动等待。重新启动只在连续运行模式下生效，`--once` 发送单帧后正常退出。以上检查只在初始化时执行；运行期间仅按帧检查重启截止时间，不增加额外 USB 请求。

在 Windows 命令末尾添加 `--hide` 可隐藏运行窗口，例如 `py .\vk_b240w.py play .\video.mp4 --cache-disk --hide`。若当前由 `python.exe` 启动，程序会使用同目录下的 `pythonw.exe` 在无窗口模式下重新启动自身，并将标准输入、输出和错误输出隐藏；默认运行日志仍会写入文件。若同时使用 `--restart-after`，重启会保留 `--hide` 参数并继续通过 `pythonw.exe` 无窗口运行。正常以 `pythonw.exe` 启动时不需要再切换解释器。`--hide` 仅支持 Windows。

视频播放会在 FFmpeg 内部将帧缩放并填充为 320×240，并在需要时限制帧率，以降低 CPU 开销。RGB 到 UYVY 的转换使用 NumPy 向量化实现，同时保持现有的像素编码结果。FFmpeg 由 `imageio-ffmpeg` 提供。请使用 `py -m pip install -r requirements.txt` 安装所需依赖。

每次运行前，驱动都会通过 HID Feature Report 识别 MS912x 芯片、选择 MS912x 状态寄存器、启动并准备视频通路，并配置抓包中观察到的 320×240 UYVY 输入。输出 VIC 设置为 `0x94`（148），这是本地 MythCool MSDevice Config 日志中列出的 320×240 时序（`htotal=568`，`vtotal=452`）；此前错误的 VIC `0` 无法正确显示图像。首次初始化 UYVY 时，驱动会先用 MS912x 启动流程中的 1080p 模式预热控制器，然后再切换到 320×240。发送帧后，驱动会触发帧传输，并启用端口 5 的屏幕闸门/视频输出。写入前会检查接口 3 和 Bulk OUT 端点 `0x04` 是否存在。驱动会先异步提交抓包中的三段帧数据，再等待传输完成，随后发送每帧后抓取到的零长度 Bulk OUT 写入。如果 PyUSB、hidapi、捆绑的视频解码器或 `libusb0.dll` 后端不可用、设备无法访问，或 USB 写入不完整，程序会报告错误。

图片和视频帧会完整适配屏幕，不会裁切；空余区域填充黑色。UYVY 编码器使用控制器的有限范围亮度系数，并像分析得到的 MS912x 编码器一样，对每个 2×2 像素块的色度取平均值。如果系统中存在支持中文的 Windows 字体，文字会优先使用该字体；也可以通过 `--font` 指定其他 `.ttf` 或 `.ttc` 字体文件。

## USB 传输超时排查

对早期未初始化的发送程序进行实时 USBPcap 抓包时，发现三个帧数据块都已提交到端点 `0x04`，但没有成功完成；5 秒后，请求以状态 `0xC0010000` 被取消。之后通过只读 HID 查询确认控制器为 MS912C，视频端口为 5。随后使用 MS912x 命令序列补上了缺失的 HID 控制面初始化。本地 MythCool 日志显示 320×240 输出模式的 VIC 为 148（`0x94`），因此替换了之前错误的 VIC 0。更正时序后，已确认校准图、文字和普通图片都能正常显示。控制器的帧触发指定了 100 ms 延迟，因此默认刷新率为 5 FPS。官方抓包在第一帧之前包含一次零长度写入，并在之后每帧之间各有一次；首次写入可能是抓包开始前已启动帧的结束标记。程序会在每帧后发送结束标记。

## 测试

```powershell
py -m unittest -v
```
