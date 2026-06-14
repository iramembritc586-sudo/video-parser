"""视频地址解析后端。

提供两种方式从网页中提取真实视频流地址：
1. yt-dlp 引擎：支持上千个网站，能拿到结构化的清晰度/格式信息（首选）。
2. 原始网页扫描：直接抓 HTML 并正则匹配 m3u8/mp4 直链，作为 yt-dlp 不支持时的兜底。
"""

from __future__ import annotations

import re
import gzip
import io
from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.request import Request, urlopen


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class Stream:
    """一条可下载/播放的视频流。"""
    url: str
    format_id: str = ""
    ext: str = ""
    resolution: str = ""      # 如 1920x1080 或 1080p
    note: str = ""            # 清晰度描述，如 "1080p60"
    vcodec: str = ""
    acodec: str = ""
    tbr: float = 0.0          # 总码率 kbps
    filesize: int = 0         # 字节，0 表示未知
    protocol: str = ""
    source: str = ""          # "yt-dlp" / "raw" / "bilibili" / "direct"
    referer: str = ""         # 下载/播放时需要的 Referer（如 B 站 CDN 要求）
    audio_url: str = ""       # DASH 视频流配套的音频地址，下载时合并

    @property
    def kind(self) -> str:
        has_v = self.vcodec not in ("", "none")
        has_a = self.acodec not in ("", "none")
        if has_v and has_a:
            return "音视频"
        if has_v:
            return "仅视频"
        if has_a:
            return "仅音频"
        return "未知"

    @property
    def size_human(self) -> str:
        if not self.filesize:
            return "-"
        n = float(self.filesize)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}PB"


@dataclass
class ParseResult:
    title: str = ""
    webpage_url: str = ""
    duration: int = 0          # 秒
    thumbnail: str = ""
    streams: list[Stream] = field(default_factory=list)
    entries: list["ParseResult"] = field(default_factory=list)  # 播放列表子项
    error: str = ""

    @property
    def duration_human(self) -> str:
        if not self.duration:
            return ""
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_to_stream(f: dict) -> Stream | None:
    url = f.get("url")
    if not url:
        return None
    h = f.get("height")
    w = f.get("width")
    if w and h:
        resolution = f"{w}x{h}"
    elif h:
        resolution = f"{h}p"
    else:
        resolution = ""
    return Stream(
        url=url,
        format_id=str(f.get("format_id", "")),
        ext=f.get("ext", "") or "",
        resolution=resolution,
        note=f.get("format_note", "") or "",
        vcodec=f.get("vcodec", "") or "",
        acodec=f.get("acodec", "") or "",
        tbr=float(f.get("tbr") or 0),
        filesize=int(f.get("filesize") or f.get("filesize_approx") or 0),
        protocol=f.get("protocol", "") or "",
        source="yt-dlp",
    )


def _info_to_result(info: dict) -> ParseResult:
    res = ParseResult(
        title=info.get("title", "") or "",
        webpage_url=info.get("webpage_url", "") or info.get("original_url", "") or "",
        duration=int(info.get("duration") or 0),
        thumbnail=info.get("thumbnail", "") or "",
    )
    fmts = info.get("formats") or []
    all_streams = [s for s in (_fmt_to_stream(f) for f in fmts) if s]

    # 区分：纯视频 / 纯音频 / 音视频合一
    video_only = [s for s in all_streams
                  if s.vcodec not in ("", "none") and s.acodec in ("", "none")]
    audio_only = [s for s in all_streams
                  if s.acodec not in ("", "none") and s.vcodec in ("", "none")]
    muxed = [s for s in all_streams
             if s.vcodec not in ("", "none") and s.acodec not in ("", "none")]

    best_audio = max(audio_only, key=lambda a: (a.tbr, a.filesize)) \
        if audio_only else None

    if video_only and best_audio:
        # 给纯视频流挂上最佳音频，下载/播放时自动合并；隐藏单独音频行
        for s in video_only:
            s.audio_url = best_audio.url
            s.acodec = best_audio.acodec   # 标记为含音频，类型显示「音视频」
        res.streams = video_only + muxed
    else:
        res.streams = all_streams  # 没有可合并的音频就原样保留

    # 顶层直链兜底
    if not res.streams and info.get("url"):
        s = _fmt_to_stream(info)
        if s:
            res.streams.append(s)

    res.streams.sort(key=lambda s: (s.tbr, s.filesize), reverse=True)
    return res


def parse_with_ytdlp(url: str, log=lambda m: None) -> ParseResult:
    """用 yt-dlp 提取（不下载）。"""
    import yt_dlp

    class _Logger:
        def debug(self, m):
            if m.startswith("[debug]"):
                return
            log(m)
        def info(self, m): log(m)
        def warning(self, m): log("⚠ " + m)
        def error(self, m): log("✗ " + m)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,        # 默认只解析单个视频，避免整个播放列表卡很久
        "playlist_items": "1",
        "logger": _Logger(),
        "extract_flat": False,
        "socket_timeout": 20,      # 网络超时，避免无限挂起
    }
    log("用 yt-dlp 解析中…")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") == "playlist" or info.get("entries"):
        top = ParseResult(
            title=info.get("title", "") or "播放列表",
            webpage_url=info.get("webpage_url", "") or url,
        )
        for e in info.get("entries") or []:
            if e:
                top.entries.append(_info_to_result(e))
        # 把第一个子项的流也提上来方便直接看
        if top.entries:
            top.streams = top.entries[0].streams
        return top

    return _info_to_result(info)


def _fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    """返回 (最终URL, 文本内容)。"""
    req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096])
    if m:
        charset = m.group(1).decode("ascii", "ignore")
    return final, raw.decode(charset, "ignore")


_URL_RE = re.compile(
    r"""https?://[^\s"'\\<>()]+?\.(?:m3u8|mp4|flv|webm|ts|mpd)(?:\?[^\s"'\\<>()]*)?""",
    re.IGNORECASE,
)
# 也匹配 JSON/JS 里被转义的地址 http:\/\/...
_ESC_URL_RE = re.compile(
    r"""https?:\\?/\\?/[^\s"'<>]+?\.(?:m3u8|mp4|flv|webm|mpd)(?:\?[^\s"'<>]*)?""",
    re.IGNORECASE,
)


def parse_raw(url: str, log=lambda m: None) -> ParseResult:
    """直接抓网页并正则扫描视频直链，作为兜底。"""
    log("抓取网页源码扫描直链…")
    final, html = _fetch(url)
    res = ParseResult(webpage_url=final)

    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if tm:
        res.title = re.sub(r"\s+", " ", tm.group(1)).strip()

    found: dict[str, Stream] = {}
    candidates = set(_URL_RE.findall(html))
    for raw_u in _ESC_URL_RE.findall(html):
        candidates.add(raw_u.replace("\\/", "/").replace("\\", ""))

    # <video src> / <source src> / <a href> 相对地址
    for m in re.finditer(
            r"""<(?:video|source)[^>]+src=["']([^"']+)["']""", html, re.IGNORECASE):
        candidates.add(urljoin(final, m.group(1)))
    for m in re.finditer(r"""<a[^>]+href=["']([^"']+?\.(?:mp4|m3u8|flv|webm|mpd))["']""",
                         html, re.IGNORECASE):
        candidates.add(urljoin(final, m.group(1)))

    for u in candidates:
        u = u.strip()
        if not u or u in found:
            continue
        ext = re.search(r"\.(m3u8|mp4|flv|webm|ts|mpd)", u, re.IGNORECASE)
        found[u] = Stream(
            url=u,
            ext=ext.group(1).lower() if ext else "",
            protocol="hls" if (ext and ext.group(1).lower() == "m3u8") else "https",
            source="raw",
        )

    res.streams = list(found.values())
    log(f"扫描到 {len(res.streams)} 个候选直链")
    return res


def find_chrome() -> str:
    """定位本机 Chrome/Chromium 可执行文件，找不到返回空串。"""
    import os
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    for p in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if os.path.exists(p):
            return p
    return ""


_MEDIA_URL_RE = re.compile(
    r"""https?://[^\s"'\\<>]+?\.(?:m3u8|mp4|flv|m4s|ts)(?:\?[^\s"'\\<>]*)?""",
    re.IGNORECASE)


def parse_headless(url: str, log=lambda m: None, wait_ms: int = 22000) -> ParseResult:
    """用无头 Chrome 真正加载页面、跑 JS，从网络请求里捕获视频流地址。
    适用于 yt-dlp/正则都搞不定的 JS 动态网站（如各类短剧站）。"""
    import os
    import shutil
    import subprocess
    import tempfile
    from urllib.parse import urlparse

    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("未找到 Chrome/Chromium，无法用无头模式解析")

    tmp = tempfile.mkdtemp(prefix="vp_hl_")
    netlog = os.path.join(tmp, "net.json")
    try:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--mute-audio", "--disable-extensions",
               f"--user-data-dir={os.path.join(tmp, 'prof')}",
               f"--log-net-log={netlog}",
               "--net-log-capture-mode=IncludeSensitive",
               "--autoplay-policy=no-user-gesture-required",
               f"--virtual-time-budget={wait_ms}", url]
        log(f"用无头浏览器加载页面并捕获视频请求（约 {wait_ms // 1000} 秒）…")
        try:
            subprocess.run(cmd, capture_output=True,
                           timeout=wait_ms / 1000 + 20)
        except subprocess.TimeoutExpired:
            pass  # 超时也可能已经抓到，继续解析 netlog

        raw = ""
        if os.path.exists(netlog):
            raw = open(netlog, encoding="utf-8", errors="ignore").read()

        origin = "{0.scheme}://{0.netloc}/".format(urlparse(url))
        found: dict[str, Stream] = {}
        for u in _MEDIA_URL_RE.findall(raw):
            key = u.split("?")[0]
            # 只保留"路径本身"以视频扩展名结尾的，排除把视频地址塞进查询串的统计信标
            if not key.lower().endswith((".m3u8", ".mp4", ".flv", ".m4s", ".ts")):
                continue
            if key in found:
                continue
            ext = key.rsplit(".", 1)[-1].lower()
            found[key] = Stream(
                url=u, ext=ext,
                protocol="hls" if ext == "m3u8" else "https",
                referer=origin, source="browser")
        res = ParseResult(webpage_url=url, streams=list(found.values()))
        # m3u8 优先（通常是完整流），mp4 次之
        res.streams.sort(key=lambda s: (s.ext != "m3u8", len(s.url)))
        log(f"✓ 无头浏览器捕获到 {len(res.streams)} 个视频流")
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- B 站原生解析（绕开 yt-dlp 的 412 风控） ----------------
_BILI_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
               43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48,
               7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54,
               21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_BILI_REFERER = "https://www.bilibili.com/"


def _bili_get(url: str, cookie: str = "") -> dict:
    import json
    headers = {"User-Agent": UA, "Referer": _BILI_REFERER}
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as r:
        return json.load(r)


def bili_cookie_from_browser(browser: str = "chrome", log=lambda m: None) -> str:
    """从浏览器读取 B 站登录 Cookie（SESSDATA 等），用于解锁高清。读不到返回空串。"""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
        want = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")
        pairs = {c.name: c.value for c in jar
                 if "bilibili.com" in (c.domain or "") and c.name in want}
        if "SESSDATA" not in pairs:
            log(f"{browser} 里没找到 B 站登录态（SESSDATA），将用未登录画质")
            return ""
        log(f"已读取 {browser} 的 B 站登录 Cookie，可解锁登录画质")
        return "; ".join(f"{k}={v}" for k, v in pairs.items())
    except Exception as e:  # noqa: BLE001
        log(f"读取浏览器 Cookie 失败：{e}")
        return ""


# ---- 持久化登录态：扫码登录一次，存本地，以后自动加载 ----
import os
import json as _json

if os.name == "nt":  # Windows 放 %APPDATA%
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                              "video-parser")
else:
    CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "video-parser")
BILI_COOKIE_FILE = os.path.join(CONFIG_DIR, "bili_login.json")


def bili_load_cookie() -> str:
    """读取本地保存的 B 站登录 Cookie，没有则返回空串。"""
    try:
        with open(BILI_COOKIE_FILE, encoding="utf-8") as f:
            return _json.load(f).get("cookie", "")
    except (OSError, ValueError):
        return ""


def bili_save_cookie(cookie: str):
    import time
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(BILI_COOKIE_FILE, "w", encoding="utf-8") as f:
        _json.dump({"cookie": cookie, "saved_at": int(time.time())}, f)
    try:
        os.chmod(BILI_COOKIE_FILE, 0o600)  # 含登录态，仅本人可读
    except OSError:
        pass


def bili_logout():
    try:
        os.remove(BILI_COOKIE_FILE)
    except OSError:
        pass


def bili_login_status(cookie: str = None) -> tuple[bool, str]:
    """返回 (是否已登录, 用户名/提示)。"""
    if cookie is None:
        cookie = bili_load_cookie()
    if not cookie:
        return False, "未登录"
    try:
        d = _bili_get("https://api.bilibili.com/x/web-interface/nav", cookie)["data"]
        if d.get("isLogin"):
            vip = "（大会员）" if d.get("vipStatus") else ""
            return True, d.get("uname", "已登录") + vip
        return False, "登录已过期，请重新登录"
    except Exception:  # noqa: BLE001
        return False, "无法验证登录状态"


def bili_qr_login(on_qr_url, should_stop=lambda: False, log=lambda m: None) -> str:
    """B 站扫码登录。on_qr_url(url) 回调用于展示二维码；返回登录 Cookie（失败返回空）。"""
    import time
    import http.cookiejar
    from urllib.request import build_opener, HTTPCookieProcessor

    gen = _bili_get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    if gen.get("code") != 0:
        log("生成二维码失败")
        return ""
    key = gen["data"]["qrcode_key"]
    on_qr_url(gen["data"]["url"])
    log("二维码已生成，请用手机 B 站 App 扫码并确认登录…")

    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    poll = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
            f"?qrcode_key={key}")
    while not should_stop():
        time.sleep(2)
        req = Request(poll, headers={"User-Agent": UA, "Referer": _BILI_REFERER})
        with opener.open(req, timeout=20) as r:
            data = _json.load(r)["data"]
        code = data.get("code")
        if code == 0:  # 登录成功，cookie 已进 jar
            want = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
            pairs = {c.name: c.value for c in jar if c.name in want}
            cookie = "; ".join(f"{k}={v}" for k, v in pairs.items())
            if cookie:
                bili_save_cookie(cookie)
                log("✓ 登录成功，已保存登录态（以后自动使用）")
            return cookie
        elif code == 86038:  # 二维码失效
            log("二维码已失效，请重新登录")
            return ""
        elif code == 86090:  # 已扫码待确认
            log("已扫码，请在手机上确认…")
        # 86101 = 未扫码，继续等
    return ""


def _bili_wbi_key() -> str:
    d = _bili_get("https://api.bilibili.com/x/web-interface/nav")["data"]["wbi_img"]
    raw = (d["img_url"].rsplit("/", 1)[-1].split(".")[0] +
           d["sub_url"].rsplit("/", 1)[-1].split(".")[0])
    return "".join(raw[i] for i in _BILI_MIXIN)[:32]


def _bili_sign(params: dict, mixin_key: str) -> str:
    import time, hashlib
    from urllib.parse import quote, urlencode
    params["wts"] = int(time.time())
    q = "&".join(f"{k}={quote(str(params[k]), safe='')}" for k in sorted(params))
    params["w_rid"] = hashlib.md5((q + mixin_key).encode()).hexdigest()
    return urlencode(params)


def is_bilibili(url: str) -> bool:
    return bool(re.search(r"(?:^|\.)bilibili\.com/", url, re.IGNORECASE)) or \
        bool(re.search(r"\bBV[0-9A-Za-z]{8,12}\b", url))


_BILI_QN_MAP = {16: "360p", 32: "480p", 64: "720p", 74: "720p60", 80: "1080p",
                112: "1080p+", 116: "1080p60", 120: "4K", 125: "HDR",
                126: "杜比视界", 127: "8K"}


def _bili_pick_url(entry: dict) -> str:
    """优先选稳定的 upos 主节点，避开 mcdn/PCDN（常拉不动导致没声音）。"""
    urls = [entry.get("baseUrl") or entry.get("base_url") or ""]
    urls += entry.get("backupUrl") or entry.get("backup_url") or []
    urls = [u for u in urls if u]
    stable = [u for u in urls if "mcdn." not in u and "/pcdn" not in u
              and ".szbdyd." not in u]
    return (stable or urls)[0] if urls else ""


def _bili_fill_streams(res: ParseResult, data: dict):
    """把 playurl 返回的 dash/durl 转成 Stream（视频流自带音频合并）。"""
    if "dash" in data and data["dash"]:
        dash = data["dash"]
        audios = dash.get("audio") or []
        best_audio = _bili_pick_url(max(audios, key=lambda a: a.get("bandwidth", 0))) \
            if audios else ""
        for v in dash.get("video", []):
            res.streams.append(Stream(
                url=_bili_pick_url(v),
                ext="mp4",
                resolution=f"{v.get('width')}x{v.get('height')}",
                note=_BILI_QN_MAP.get(v.get("id"), ""),
                vcodec=v.get("codecs", ""),
                acodec=(audios[0].get("codecs", "mp4a") if audios else "none"),
                tbr=round(v.get("bandwidth", 0) / 1000),
                protocol="dash", source="bilibili", referer=_BILI_REFERER,
                audio_url=best_audio,   # 下载/播放时自动合并
            ))
    elif "durl" in data and data["durl"]:  # 老视频/部分番剧：整段 mp4/flv
        for d in data["durl"]:
            res.streams.append(Stream(
                url=d["url"], ext="flv",
                note=_BILI_QN_MAP.get(data.get("quality"), ""),
                vcodec="h264", acodec="aac",
                protocol="https", source="bilibili", referer=_BILI_REFERER,
            ))
    res.streams.sort(key=lambda s: (s.vcodec != "none", s.tbr), reverse=True)


def _parse_bili_bangumi(url: str, log, cookie: str) -> ParseResult:
    """B 站番剧/影视（ep/ss 号）解析，走 pgc 接口，带登录态可解锁会员清晰度。"""
    m = re.search(r"\b(ep|ss)(\d+)\b", url, re.IGNORECASE)
    if not m:
        raise ValueError("未识别番剧 ep/ss 号")
    kind, num = m.group(1).lower(), m.group(2)
    log(f"B 站番剧解析 {kind}{num}…" + ("（已登录）" if cookie else "（未登录）"))
    q = f"ep_id={num}" if kind == "ep" else f"season_id={num}"
    season = _bili_get(f"https://api.bilibili.com/pgc/view/web/season?{q}", cookie)
    if season.get("code") != 0:
        raise RuntimeError(f"获取番剧信息失败：{season.get('message')}")
    sr = season["result"]
    eps = sr.get("episodes") or []
    if not eps:
        raise RuntimeError("该番剧没有可播放的剧集")
    # ep 链接定位到具体一集；ss 链接默认取第 1 集
    ep = next((e for e in eps if str(e.get("id")) == num), eps[0]) \
        if kind == "ep" else eps[0]
    ep_id, cid = ep.get("id"), ep.get("cid")
    title = f"{sr.get('title', '')} {ep.get('title', '')} {ep.get('long_title', '')}".strip()

    play = _bili_get(
        "https://api.bilibili.com/pgc/player/web/playurl?"
        f"ep_id={ep_id}&cid={cid}&qn=127&fnval=4048&fourk=1", cookie)
    if play.get("code") != 0:
        raise RuntimeError(f"获取播放地址失败：{play.get('message')}")
    pdata = play.get("result") or play.get("data") or {}

    res = ParseResult(title=title, webpage_url=f"https://www.bilibili.com/bangumi/play/ep{ep_id}",
                      duration=int((ep.get("duration") or 0) / 1000))
    _bili_fill_streams(res, pdata)
    if not res.streams:
        raise RuntimeError("未取得可用清晰度（可能是会员专享，需登录会员账号）")
    log(f"✓ 番剧解析成功，共 {len(res.streams)} 个流")
    return res


def parse_bilibili(url: str, log=lambda m: None, cookie: str = "") -> ParseResult:
    """直接调 B 站接口拿播放地址。带登录 cookie 可解锁高清/会员内容。"""
    # 番剧/影视用 ep/ss 号，走 pgc 接口
    if re.search(r"/bangumi/|\b(?:ep|ss)\d+\b", url, re.IGNORECASE):
        return _parse_bili_bangumi(url, log, cookie)

    m = re.search(r"\b(BV[0-9A-Za-z]{8,12})\b", url)
    if not m:
        raise ValueError("未能从地址中识别 BV 号")
    bvid = m.group(1)
    log(f"B 站原生解析 {bvid}…" + ("（已登录）" if cookie else "（未登录）"))
    view = _bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                     cookie)
    if view.get("code") != 0:
        raise RuntimeError(f"获取视频信息失败：{view.get('message')}")
    vd = view["data"]
    cid, avid = vd["cid"], vd["aid"]

    mixin = _bili_wbi_key()
    qs = _bili_sign({"avid": avid, "cid": cid, "qn": 127, "fnval": 4048, "fourk": 1},
                    mixin)
    play = _bili_get(f"https://api.bilibili.com/x/player/wbi/playurl?{qs}", cookie)
    if play.get("code") != 0:
        raise RuntimeError(f"获取播放地址失败：{play.get('message')}")

    res = ParseResult(
        title=vd.get("title", ""),
        webpage_url=f"https://www.bilibili.com/video/{bvid}",
        duration=int(vd.get("duration") or 0),
        thumbnail=vd.get("pic", ""),
    )
    _bili_fill_streams(res, play["data"])
    log(f"✓ B 站解析成功，共 {len(res.streams)} 个流")
    return res


def normalize_url(url: str, log=lambda m: None) -> str:
    """把某些信息流/弹窗地址改写成 yt-dlp 能识别的标准视频地址。"""
    from urllib.parse import urlparse, parse_qs

    p = urlparse(url)
    host = p.netloc.lower()
    qs = parse_qs(p.query)

    # 抖音精选/推荐弹窗：douyin.com/jingxuan?modal_id=<id> -> /video/<id>
    if "douyin.com" in host and "modal_id" in qs:
        vid = qs["modal_id"][0]
        new = f"https://www.douyin.com/video/{vid}"
        log(f"识别为抖音 modal 地址，改写为 {new}")
        return new

    # 小红书 explore 弹窗等其它带 modal_id 的情况，统一兜底成 /video/
    if "modal_id" in qs and re.fullmatch(r"\d+", qs["modal_id"][0] or ""):
        vid = qs["modal_id"][0]
        new = f"{p.scheme}://{p.netloc}/video/{vid}"
        log(f"检测到 modal_id，尝试改写为 {new}")
        return new

    return url


def parse(url: str, log=lambda m: None, bili_cookie: str = "") -> ParseResult:
    """主入口：先 yt-dlp，失败再原始扫描。bili_cookie 用于解锁 B 站高清。"""
    url = url.strip()
    if not url:
        return ParseResult(error="请输入网址")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    url = normalize_url(url, log)

    # 网址本身就是直链媒体文件：直接当作一条流返回
    direct = re.search(r"\.(m3u8|mp4|flv|webm|ts|mpd)(?:\?|$)", url, re.IGNORECASE)
    if direct:
        ext = direct.group(1).lower()
        log(f"✓ 检测到直链媒体文件（.{ext}）")
        return ParseResult(
            title=url.rsplit("/", 1)[-1].split("?")[0],
            webpage_url=url,
            streams=[Stream(
                url=url, ext=ext,
                protocol="hls" if ext == "m3u8" else "https",
                source="direct")],
        )

    # B 站走原生解析（yt-dlp 常被 412 拦），失败再退回 yt-dlp
    if is_bilibili(url):
        cookie = bili_cookie or bili_load_cookie()  # 优先用已保存的登录态
        try:
            res = parse_bilibili(url, log, cookie=cookie)
            if res.streams:
                return res
            log("B 站原生解析未返回流，改用 yt-dlp…")
        except Exception as e:  # noqa: BLE001
            log(f"B 站原生解析失败：{e}，改用 yt-dlp…")

    try:
        res = parse_with_ytdlp(url, log)
        if res.streams or res.entries:
            log(f"✓ yt-dlp 解析成功，共 {len(res.streams)} 个视频流")
            return res
        log("yt-dlp 未返回视频流，尝试原始扫描…")
    except Exception as e:  # noqa: BLE001
        log(f"yt-dlp 解析失败：{e}")
        log("回退到原始网页扫描…")

    try:
        res = parse_raw(url, log)
        if res.streams:
            return res
        log("原始扫描无果，尝试无头浏览器捕获（JS 动态站）…")
    except Exception as e:  # noqa: BLE001
        log(f"原始扫描失败：{e}")

    # 最后兜底：无头 Chrome 真正跑 JS，捕获网络里的视频流
    try:
        res = parse_headless(url, log)
        if res.streams:
            return res
        return ParseResult(error="未捕获到视频流（可能需要登录/付费，或页面无视频）")
    except Exception as e:  # noqa: BLE001
        return ParseResult(error=f"解析失败：{e}")
