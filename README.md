# 网页视频地址提取

输入网页地址，提取真实视频流（m3u8 / mp4 等），可在线播放或下载到本地
（视频/音频自动合并）。支持 YouTube / B站（含番剧）/ 抖音 等上千网站。

提供两种界面：**网页版**（推荐，跨平台 Windows/Mac/Linux）和**桌面版**（tkinter）。

## 运行 —— 网页版（推荐，跨平台）

浏览器界面，Windows / Mac / Linux 都能用。

- **Linux / macOS**：`./run-web.sh`
- **Windows**：双击 `run-web.bat`

首次运行会自动建虚拟环境、装依赖，然后开服务并打开浏览器（地址 http://127.0.0.1:8731 ）。

> 需先装好 [Python 3](https://www.python.org/) 和 [ffmpeg](https://ffmpeg.org/)（下载合并必需）。
> 可选：[mpv](https://mpv.io/)（本机播放器）、[aria2](https://aria2.github.io/)（多线程加速下载）。
> Windows 把 ffmpeg/aria2 的 exe 放进 PATH 即可。

## 运行 —— 桌面版（tkinter）

```bash
./run.sh          # 或 .venv/bin/python app.py
```

## 功能

- **解析**：粘贴网页地址 → 点「解析」，列出该页所有可用视频流（清晰度 / 格式 / 类型 / 码率 / 大小 / 协议）。
- **复制地址 / 浏览器打开 / 系统播放器播放**：选中某条流后操作（装了 mpv 或 vlc 会优先用它播放）。
- **下载选中**：yt-dlp 来源的流按格式下载并自动合并音视频；直链 / m3u8 用 ffmpeg 下载合并。

## 解析原理

0. **B 站原生解析**：B 站地址直接调官方 WBI 接口拿播放地址，绕开 yt-dlp 常遇到的
   412 风控，**无需登录 / cookie**。视频音频分离（DASH），下载时自动用 ffmpeg 合并。
   未登录画质上限约 1080p，更高需登录账号。
1. **yt-dlp 引擎**（首选）：支持上千个网站，返回结构化的多清晰度视频流。
2. **原始网页扫描**（兜底）：yt-dlp 不支持时，直接抓 HTML 正则匹配 `<video>`/`<source>`/`<a>`
   标签和 JS 中的 `.m3u8`/`.mp4` 等直链。
3. **直链短路**：网址本身就是 `.mp4`/`.m3u8` 文件时直接作为一条流返回。

## B 站登录（解锁高清，一次永久）

点界面上的「**登录B站**」按钮 → 弹出二维码 → 手机 B 站 App 扫码确认。
登录态会保存在 `~/.config/video-parser/bili_login.json`（权限 600，仅本人可读），
**以后每次启动自动加载，无需再次登录**，直到登录态过期或手动「重新登录」。

- 未登录：B 站最高约 480P（受限）
- 登录后：1080P；大会员可到 4K / HDR / 杜比视界

## 依赖

- **Python 3**
- **ffmpeg**（下载合并必需）
- Python 包：见 `requirements.txt`（yt-dlp / Flask / qrcode 等，运行脚本会自动装）
- 可选：**mpv**（桌面版本机播放）、**aria2**（多线程加速下载）

手动安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # Windows: .venv\Scripts\pip install -r requirements.txt
```

更新 yt-dlp（网站改版导致解析失败时）：

```bash
.venv/bin/pip install -U yt-dlp
```

### Windows 提示

- 装 [Python](https://www.python.org/)（勾选 Add to PATH）和 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)。
- 双击 `run-web.bat` 即可，首次会自动装依赖。
- B 站登录态存在 `%APPDATA%\video-parser\`。

## 说明

请仅用于提取你有权访问 / 下载的内容，遵守目标网站的服务条款与版权规定。
