# qrstream / qrrecv

把任意文件编码成**循环播放的动画二维码**，在终端里显示；手机浏览器打开
[RaptorQR 接收端](https://qr.linkto.host/)，用相机对着终端扫描，即可在手机上
还原并下载原文件。也可以把二维码流录成视频，事后用 **qrrecv** 离线解析还原。

- **发送端单文件、零第三方依赖**：只用 Python 标准库，`python qrstream.py 文件` 即可运行。
- **视频接收端**：`python qrrecv.py demo.mp4` 即可从视频/录屏/帧目录中扫出二维码并还原文件；
  提供自带 OpenCV + ZXing 的 `dist/qrrecv.exe`，运行时同样零外部依赖。
- **协议兼容 RaptorQR**：8 字节包头 + CRC32C + RLNC（K=16 / R=8）+ 3% 外层
  Reed-Solomon，官方接收端（FEC 设为 `Auto`）可直接解码。
- **自带 QR 编码器**：字节模式，支持版本 1–40 与 L/M/Q/H 纠错级别
  （CLI 暴露 RaptorQR 常用的 V10–V40 档位），纯 Python 实现。
- **可选 exe**：`dist/qrstream.exe` / `dist/qrrecv.exe` 可在未安装 Python 的
  Windows 上直接运行。

> 手机接收端是 RaptorQR 官方网页应用，属于“对端”，不是本程序的运行依赖。
> 发送端不上传文件、不联网、不写注册表；视频接收端只在本地读取文件。

## 文件

| 文件 | 说明 |
| --- | --- |
| `qrstream.py` | 发送端，单文件纯标准库 |
| `qrrecv.py` | 视频接收端（需 opencv-python / numpy / zxing-cpp） |
| `dist/qrstream.exe` | 发送端 Windows 独立版 |
| `dist/qrrecv.exe` | 接收端 Windows 独立版（内置 OpenCV/NumPy/ZXing，约 65 MB） |
| `README.md` | 本文档 |

---

## 快速开始

环境：Python 3.9+。发送端无需安装任何第三方包；接收端依赖见下文，
Windows 也可直接使用 `dist/qrstream.exe` 与 `dist/qrrecv.exe`。

### 1. 发送端（电脑）

```powershell
# 传输文件
python qrstream.py 简历.pdf

# 传输文本
echo "Hello, RaptorQR!" | python qrstream.py --text

# 先看看会生成多少帧、占多大屏幕
python qrstream.py --info 简历.pdf

# 想用更大的 QR、更快的吞吐
python qrstream.py --qr-version 20 --ecc L 简历.pdf
```

播放时：

| 按键 | 作用 |
| --- | --- |
| `q` / `Q` / `Esc` / `Ctrl-C` | 退出 |
| `空格` | 暂停 / 继续 |

终端会显示状态行、二维码和接收端地址。二维码循环播放，接收端可以随时开始扫描。

### 2. 接收端（手机）

1. 手机浏览器打开 <https://qr.linkto.host/>（首次打开后支持离线 PWA）。
2. 进入“接收 / Receive”，允许相机权限。
3. FEC 编码保持默认的 **Auto**（关键：本程序走的是兼容的 `js-rlnc` 路径）。
4. 把手机对准终端里的二维码，保持约 1–2 秒；进度跑满后会自动还原文件并下载。

> 如果接收端把 FEC 手动改成了 `RaptorQ WASM`，会提示收到 `js-rlnc` 包而无法解码；
> 改回 **Auto** 即可。

### 3. 离线接收端（视频解析）

如果已经有一段包含二维码流的视频（手机拍摄屏幕、录屏、或导出的视频），
可以用 `qrrecv` 离线解析：

```powershell
# 解析视频，自动按元数据里的原文件名输出
python qrrecv.py demo.mp4

# 指定输出文件
python qrrecv.py demo.mp4 -o recovered.pdf

# 直接解析发送端导出的 PNG 帧目录（不经过视频编码）
python qrrecv.py frames/ --stride 1
```

依赖说明：

| 形式 | 运行依赖 |
| --- | --- |
| `qrrecv.py` | 需要 `pip install opencv-python numpy zxing-cpp` |
| `dist/qrrecv.exe` | 已内置 OpenCV / NumPy / ZXing-C++，**无需安装任何东西** |

支持 mp4 / mov / avi / mkv / webm / gif 等 OpenCV 能解码的视频，也支持图片目录
（`--export` 的输出）和单张图片。

### 4. 不想播放，只想导出帧

```powershell
python qrstream.py 简历.pdf --export frames --scale 6
```

每帧一个 PNG（`frame_00001.png`…），附带 `manifest.json`（文件名、大小、
SHA-256、QR 配置、帧数等）。可以拿去做 GIF：

```bash
ffmpeg -framerate 10 -i frames/frame_%05d.png -loop 0 demo.gif
```

---

## 发送端参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `file` | 无 | 要传输的文件；省略时从标准输入读文本 |
| `--qr-version`, `-V` | `10` | QR 版本：10/15/20/25/30/35/40；越大每帧容量越大，屏幕占用也越大 |
| `--ecc`, `-e` | `M` | QR 纠错级别 L/M/Q/H；H 最抗污损但容量最小 |
| `--fps`, `-f` | `10` | 播放帧率，0.5–30；相机较慢时降到 6–8 更稳 |
| `--text` | 关 | 按纯文本传输（不带文件名/MIME 元数据） |
| `--no-compress` | 关 | 关闭 DEFLATE 预处理压缩 |
| `--once` | 关 | 只播一轮就退出 |
| `--export DIR` | 无 | 把每帧导出为 PNG 到目录，不播放 |
| `--scale` | `6` | 导出 PNG 时每模块像素数（1–20） |
| `--quiet` | `4` | QR 静区模块数（0–16） |
| `--info` | 关 | 只打印编码计划（帧数、分代、屏幕尺寸、理论吞吐） |
| `--about` | 关 | 版本与协议说明 |

### 每帧负载（字节）

已扣除 8 字节包头和 4 字节 CRC：

| 版本 | L | M | Q | H |
| --- | ---: | ---: | ---: | ---: |
| V10 | 259 | **201** | 139 | 107 |
| V15 | 508 | 400 | 280 | 208 |
| V20 | 846 | 654 | 470 | 370 |
| V25 | 1261 | 985 | 703 | 523 |
| V30 | 1720 | 1358 | 970 | 730 |
| V35 | 2291 | 1797 | 1271 | 971 |
| V40 | 2941 | 2319 | 1651 | 1261 |

默认 `V10-M`（201 B/帧）与官方 CLI 一致：约 65 列 × 33 行，普通终端放得下。
`V20-L`（846 B/帧）约 105 列 × 57 行，需要较大的终端/较小的字体。

## 接收端参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `source` | 无 | 视频文件，或包含帧图片的目录 |
| `--output`, `-o` | 自动 | 输出路径；`-` 表示写到标准输出 |
| `--outdir` | `.` | 未指定 `--output` 时的输出目录 |
| `--start` / `--end` | 0 / 无 | 只扫描视频的某段时间（秒）；图片目录则按帧序号 |
| `--stride` | `0` | 每 N 帧取一帧；0=自动（按视频 fps 约取 15 帧/秒），1=每帧都扫 |
| `--max-frames` | `0` | 最多扫描多少个采样帧 |
| `--rotate` | `0` | 手动把每帧旋转 0/90/180/270 度 |
| `--overwrite` | 关 | 允许覆盖已存在的输出文件 |
| `--no-progress` | 关 | 不显示实时进度 |
| `--about` | 关 | 版本与依赖说明 |

默认输出文件名取自视频里携带的原始文件名；若文件已存在会自动加 `-1` 后缀，
除非指定 `--overwrite`。

---

## 工作原理

### 传输包

```
偏移  长度  内容
0     1     magic = 0x51 ('Q')
1     4     packed word（小端）
5     3     dataLength（小端，24 位）
8     N     FEC 负载
8+N   4     CRC32C（Castagnoli，覆盖前面所有字节）
```

packed word 的位布局：

```
bits 0–11   generationIndex
bits 12–23  totalGenerations
bits 24–28  symbolIndex（0–15 系统符号，16–23 RLNC 校验符号）
bit  29     isText
bit  30     isLastGeneration
bit  31     compressed
```

### FEC

1. 预处理：文件模式先加 `[文件名长度][文件名][MIME 长度][MIME]` 元数据；
   超过 64 字节且能压小时做**裸 DEFLATE** 压缩（与 fflate 的 `deflateSync` 对齐）。
2. 按每帧负载大小切成符号，每 16 个符号一代；末代补零。
3. 每代产生 16 个系统符号 + 8 个 RLNC 校验符号。校验系数由
   `(generationIndex, codedSymbolIndex)` 经 xoshiro128\*\* 确定性生成，
   与官方 JS 实现**逐位一致**。
4. 再对“代”做 3% 的外层 GF(256) Reed-Solomon（评估点 αⁱ），
   使任意 G 代（共 G+P 代）都能恢复整个文件。
5. 每个包用选定的 QR 版本/纠错级别渲染成字节模式二维码。

接收端只依赖包内的 `dataLength`、`totalGenerations` 和实际负载长度，
所以发送端可以自由选择 QR 版本和纠错级别。

### 视频接收端怎么工作

1. **取帧**：OpenCV 解码视频（默认按约 15 帧/秒采样；`--stride 1` 逐帧）。
2. **扫码**：ZXing-C++ 在每帧中定位并解码二维码，直接取原始字节（不做文本转码，
   因此二进制文件不会被破坏）。
3. **验包**：校验 magic、包头字段与 CRC32C；按 `generationIndex:symbolIndex` 去重。
4. **RLNC 解码**：每代收到 16 个线性无关符号后用 GF(256) 高斯消元解出 K 个源符号。
5. **外层 RS**：若有整代丢失，用 3% 校验代做 Lagrange 插值恢复。
6. **还原**：拼接、按 `dataLength` 截断、裸 DEFLATE 解压、解析文件名/MIME；
   文件名会做路径穿越与 Windows 保留名过滤，再写入磁盘。

### 为什么是 `js-rlnc` 而不是 RaptorQ

RaptorQR 的默认 FEC 是 RaptorQ（RFC 6330，Rust/WASM）。在纯 Python 标准库里
完整实现 RaptorQ 的预编码与永久失活解码非常庞大；而官方接收端仍保留并
测试覆盖了兼容的 `js-rlnc` 路径（`Auto` 模式会自动识别）。本程序因此实现
`js-rlnc`，做到**包级字节一致**，同时保持单文件零依赖。

---

## 可靠性与限制

- **丢包**：RLNC 每代多出 8 个校验符号，外层再追加 3% 校验代；手机只扫到部分
  帧也能恢复。开发验证中，随机丢弃 20%–30% 帧的样例均能还原；实际能否恢复
  取决于每一代是否收到足够多的独立符号（每代至少 16 个）。
- **容量上限**：`dataLength` 是 24 位（最大 16,777,215 字节）；代索引/总代数是
  12 位（最大 4095 代）。超限时程序会提示改用更大的 QR 版本（每帧容量更大、
  需要的代数更少）。
- **速度**：单二维码 + 10 fps 的物理上限约为 `每帧字节 × fps`。
  V10-M 约 2 KiB/s，V20-L 约 8 KiB/s，V30-L 约 17 KiB/s。大文件请优先用
  更大的版本，或先用压缩包减小体积。
- **屏幕**：QR 必须完整显示，不能被状态行、窗口边框或终端滚动截断；
  终端字体建议 10–14 px，关闭“护眼/暖色”滤镜，屏幕亮度调高。
- **兼容性风险**：官方把 `js-rlnc` 标记为 deprecated。如果未来官方接收端
  移除该路径，本程序需要改为 RaptorQ 编码；可以自托管当前版本的
  `apps/web/dist` 作为固定接收端。
- **视频解析**：默认按约 15 帧/秒采样以节省时间；如果视频里二维码刷新很快、
  有运动模糊或部分帧损坏，用 `--stride 1` 逐帧扫描通常能补齐。视频必须包含
  完整二维码，不能裁掉定位角。

---

## 常见问题

**扫了很久没反应？**

1. 确认接收端 FEC 是 **Auto**。
2. 终端窗口拉到能完整显示二维码（状态行 + QR + 页脚），不要有滚动条。
3. 缩小终端字体（`Ctrl + -`）让 QR 更大。

**二维码在屏幕上闪烁/撕裂？**

把 `--fps` 降到 6–8；或在 Windows Terminal 中关闭“焦点时闪烁”类特效。
帧本身是静态渲染后缓存的。

**为什么 `--info` 显示压缩后反而更大？**

随机/已压缩数据（jpg、zip、mp4）压不动，程序会自动放弃压缩并使用原始数据；
`--no-compress` 可以跳过尝试。

**能完全离线吗？**

发送端完全离线；`qrrecv` 视频解析也完全离线。手机网页接收端首次需要联网加载，
之后是 PWA，可离线打开。如果要求端到端离线，可把官方 web 构建部署到本地静态服务器。

**视频解析不完整 / 提示“还需要更多帧”？**

1. 先试 `--stride 1` 逐帧扫描（默认会跳帧）。
2. 手机拍摄的视频可能有旋转元数据，试 `--rotate 90` / `180` / `270`。
3. 用 `--start/--end` 只扫描二维码出现的那一段，避免把片头片尾算进去。
4. 确保视频没有裁掉二维码的定位角，画面不要过度压缩/模糊。
5. 如果视频来自 RaptorQ 编码的发送端（例如官方 RaptorQ WASM），本接收端不支持，
   会明确提示切换编码方式。

**exe 被杀毒软件误报？**

PyInstaller 单文件包常见误报。可以直接用 `python qrstream.py` / `python qrrecv.py`，
或自行 `pyinstaller --onefile --console qrstream.py` 重新打包。

---

## 开发与验证

开发过程中使用官方 `@raptorqr/core` 和 ZXing-C++ 做了这些交叉验证：

- 用官方 `@raptorqr/core` 的 `packetize()` 对同一输入逐字节对比，覆盖
  V10/V20/V30、压缩开/关、随机/文本/全零数据；未压缩时**包级完全一致**，
  压缩时压缩流实现不同但接收端解码结果一致；
- 用官方 `GenerationDecoder` + `assemblePayload` 解码本程序生成的包，
  在丢弃 20%–30% 帧的情况下还原原文件；
- 把导出的 PNG 用 ZXing-C++ 逐帧解码，再交给官方解码器还原，验证
  “QR 渲染 → 扫码 → 重组”全链路；
- 覆盖 L/M/Q/H × V10–V40 的容量边界与二进制数据；
- 接收端：把导出的帧编码成 MP4（30 fps × 3 重复帧），分别做清晰、模糊+噪声+
  降亮度、旋转 90°、随机丢 35% 帧、缩放+透视变换五种视频，全部还原成功且
  SHA-256 与源文件一致；另做了 1920×1080、二维码仅占约 400 px、带透视/模糊/
  噪声的“手机拍摄”模拟视频，同样还原成功；
- 接收端外层 RS：故意从视频中删掉整整一代（24 个包），仅靠 1 个校验代仍能
  还原 110 KB 文件；
- `dist/qrrecv.exe` 在未安装 Python 依赖的打包环境中重新运行上述视频，结果一致。

物理手机扫描还受屏幕亮度、相机对焦、终端字体影响，建议实际试一次后
再根据 `--info` 调整版本和帧率；视频解析效果则取决于拍摄清晰度与二维码完整性。

---

## 致谢

- [infrost/RaptorQR](https://github.com/infrost/RaptorQR)：协议、RLNC 参数、
  外层 RS 与接收端。
- [cberner/raptorq](https://github.com/cberner/raptorq)、
  [erwanvivien/fast_qr](https://github.com/erwanvivien/fast_qr)、
  [ZXing-C++](https://github.com/zxing-cpp/zxing-cpp)：RaptorQR 使用的底层组件。
- [OpenCV](https://opencv.org/)：`qrrecv` 的视频解码；[zxing-cpp](https://github.com/zxing-cpp/zxing-cpp)
  的 Python 绑定用于逐帧二维码识别。
- QR 编码器按 ISO/IEC 18004 实现；Reed-Solomon 使用 GF(256)/0x11D。
