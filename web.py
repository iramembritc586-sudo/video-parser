"""网页版后端（Flask）。本地运行，浏览器使用，跨平台（Windows/Mac/Linux）。

复用 parser.py（解析）与 downloader.py（下载），提供：
- 解析网址、列出视频流
- 浏览器内播放（防盗链流由后端代理，分离音视频由后端 ffmpeg 实时合并）
- 服务端下载并合并，再发送到浏览器保存
- B 站扫码登录
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from flask import (Flask, Response, jsonify, request, send_file,
                   render_template, stream_with_context)

import parser as vp
import downloader

HERE = os.path.dirname(os.path.abspath(__file__))


def _venv_tool(name: str) -> str:
    """跨平台定位 venv 里的可执行文件（Windows: Scripts/*.exe；Unix: bin/*）。"""
    for sub in ("Scripts", "bin"):
        for ext in (".exe", ""):
            p = os.path.join(HERE, ".venv", sub, name + ext)
            if os.path.exists(p):
                return p
    return shutil.which(name) or name


YTDLP = _venv_tool("yt-dlp")
ARIA2 = shutil.which("aria2c")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

app = Flask(__name__)

# 单用户本地应用：用模块级状态即可
STATE = {"streams": [], "pages": [], "page": "", "title": ""}
JOBS: dict[str, dict] = {}          # 下载任务
LOGIN = {"key": "", "cookie": ""}   # B 站登录会话


# ---------------- 解析 ----------------
def _codec(vc):
    if vc.startswith("avc"):
        return "H.264"
    if vc.startswith(("hvc", "hev")):
        return "H.265"
    if "av01" in vc:
        return "AV1"
    return vc.split(".")[0] if vc and vc != "none" else ""


def _mse_type(s):
    """分离流走 MSE 播放所需的 MIME codecs 串；浏览器不支持的(如HEVC)返回空。"""
    if not s.audio_url:
        return ""
    vc, ac = s.vcodec, s.acodec
    if vc.startswith(("hev", "hvc")):
        return ""
    if vc == "vp9":
        vc = "vp09.00.40.08"
    if ac in ("", "none") or (not ac.startswith("mp4a") and "opus" not in ac):
        ac = "mp4a.40.2"
    if "opus" in ac:
        ac = "opus"
    return f'video/mp4; codecs="{vc},{ac}"'


def _stream_dict(i, s):
    return {
        "i": i,
        "quality": (f"{s.note} ({s.resolution})" if s.note and s.resolution
                    else s.note or s.resolution or "-"),
        "codec": _codec(s.vcodec) if s.vcodec not in ("", "none") else (s.ext or "-"),
        "kind": s.kind,
        "tbr": round(s.tbr) if s.tbr else None,
        "size": s.size_human,
        "ext": s.ext, "protocol": s.protocol, "source": s.source,
        "needs_referer": bool(s.referer), "split": bool(s.audio_url),
        "mse": _mse_type(s),
    }


def _parse_one(url, logs):
    box = {}

    def work():
        try:
            box["res"] = vp.parse(url, log=logs.append)
        except Exception as e:  # noqa: BLE001
            box["res"] = vp.ParseResult(error=f"解析出错：{e}")
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(60)
    if th.is_alive():
        return vp.ParseResult(error="解析超时（该网址可能不支持，或网络太慢）")
    return box.get("res") or vp.ParseResult(error="解析失败")


@app.post("/api/parse")
def api_parse():
    body = request.json or {}
    raw = body.get("url", "")
    # 支持批量：按换行/空白拆成多个网址
    urls = [u.strip() for u in _re.split(r"[\r\n]+", raw) if u.strip()]
    if not urls:
        return jsonify(error="请输入网址"), 400

    STATE["streams"] = []
    STATE["pages"] = []
    logs: list[str] = []
    videos = []
    for url in urls:
        if len(urls) > 1:
            logs.append(f"—— 解析：{url} ——")
        res = _parse_one(url, logs)
        if res.error and not res.streams:
            videos.append({"title": url[:50], "error": res.error, "streams": []})
            continue
        page = res.webpage_url or url
        sds = []
        for s in res.streams:
            gi = len(STATE["streams"])
            STATE["streams"].append(s)
            STATE["pages"].append(page)
            sds.append(_stream_dict(gi, s))
        videos.append({"title": res.title or "(无标题)",
                       "duration": res.duration_human, "streams": sds})

    STATE["title"] = videos[0]["title"] if videos else ""
    total = len(STATE["streams"])
    if total == 0:
        return jsonify(error=videos[0].get("error", "未找到视频流") if videos
                       else "未找到视频流", logs=logs, videos=videos), 200
    return jsonify(videos=videos, count=total, batch=len(urls) > 1, logs=logs)


# ---------------- 浏览器内播放 ----------------
def _stream_subprocess(cmd):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        proc.kill()


@app.get("/api/play/<int:i>")
def api_play(i):
    if i >= len(STATE["streams"]):
        return "无效的流", 404
    s = STATE["streams"][i]
    headers = {"User-Agent": vp.UA}
    if s.referer:
        headers["Referer"] = s.referer

    if s.audio_url:
        # 分离音视频：ffmpeg 实时合并成分片 mp4 边合边发，浏览器可直接播放
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd = [FFMPEG, "-loglevel", "error",
               "-headers", hdr, "-i", s.url,
               "-headers", hdr, "-i", s.audio_url,
               "-map", "0:v", "-map", "1:a", "-c", "copy",
               "-movflags", "frag_keyframe+empty_moov+default_base_moof",
               "-f", "mp4", "pipe:1"]
        return Response(stream_with_context(_stream_subprocess(cmd)),
                        mimetype="video/mp4")

    # 单条流：代理转发（带 Referer，支持 Range 拖动）
    import urllib.request
    req = urllib.request.Request(s.url, headers=headers)
    rng = request.headers.get("Range")
    if rng:
        req.add_header("Range", rng)
    upstream = urllib.request.urlopen(req, timeout=30)
    status = upstream.status
    resp_headers = {"Accept-Ranges": "bytes"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            resp_headers[h] = upstream.headers[h]
    resp_headers.setdefault("Content-Type", "video/mp4")

    def gen():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()
    return Response(stream_with_context(gen()), status=status, headers=resp_headers)


# ---------------- 下载（服务端下载合并 → 发给浏览器） ----------------
import re as _re
_PCT_RES = [
    _re.compile(r"\((\d{1,3})%\)"),              # aria2c: (24%)
    _re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d)?)%"),  # yt-dlp: [download] 45.2%
]


def _parse_percent(line):
    for rx in _PCT_RES:
        m = rx.search(line)
        if m:
            try:
                return min(100, int(float(m.group(1))))
            except ValueError:
                pass
    return None


def _download_job(job_id, s, page, out):
    job = JOBS[job_id]

    def on_log(m):
        job["line"] = m[:120]
        p = _parse_percent(m)
        if p is not None:
            job["percent"] = p

    try:
        ok = downloader.download(
            s, page, out, log=on_log,
            ytdlp=YTDLP, aria2=ARIA2,
            status=lambda m: (job.update(status=m), job.update(percent=None)))
        job["state"] = "done" if ok and os.path.exists(out) else "failed"
        job["file"] = out if ok else ""
        if job["state"] == "done":
            job["percent"] = 100
    except Exception as e:  # noqa: BLE001
        job["state"] = "failed"
        job["line"] = str(e)[:120]


def _start_job(i):
    if i >= len(STATE["streams"]):
        return None
    s = STATE["streams"][i]
    page = STATE["pages"][i] if i < len(STATE.get("pages", [])) else STATE.get("page", "")
    job_id = uuid.uuid4().hex
    safe = "".join(c for c in (STATE["title"] or "video")
                   if c.isalnum() or c in " _-")[:60].strip() or "video"
    tmpdir = tempfile.mkdtemp(prefix="vpweb_")
    out = os.path.join(tmpdir, f"{safe}.mp4")
    JOBS[job_id] = {"state": "running", "status": "开始下载…", "line": "",
                    "name": os.path.basename(out)}
    threading.Thread(target=_download_job, args=(job_id, s, page, out),
                     daemon=True).start()
    return job_id


@app.post("/api/download/<int:i>")
def api_download(i):
    job_id = _start_job(i)
    return (jsonify(job=job_id) if job_id else (jsonify(error="无效的流"), 404))


@app.get("/api/download/status/<job_id>")
def api_dl_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="任务不存在"), 404
    return jsonify(state=job["state"], status=job.get("status", ""),
                   line=job.get("line", ""), name=job.get("name", ""),
                   percent=job.get("percent"))


@app.get("/api/download/file/<job_id>")
def api_dl_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["state"] != "done" or not job.get("file"):
        return "文件未就绪", 404
    return send_file(job["file"], as_attachment=True,
                     download_name=job["name"])


# ---------------- 在线预览（分离流：下载合并后用普通 mp4 播放，最可靠） ----------------
@app.post("/api/preview/<int:i>")
def api_preview(i):
    job_id = _start_job(i)
    return (jsonify(job=job_id) if job_id else (jsonify(error="无效的流"), 404))


@app.get("/api/preview/file/<job_id>")
def api_preview_file(job_id):
    job = JOBS.get(job_id)
    if not job or job["state"] != "done" or not job.get("file"):
        return "未就绪", 404
    # conditional=True 支持 Range，浏览器可拖动进度
    return send_file(job["file"], mimetype="video/mp4", conditional=True)


# ---------------- B 站扫码登录 ----------------
@app.get("/api/bili/status")
def api_bili_status():
    ok, name = vp.bili_login_status()
    return jsonify(logged_in=ok, name=name)


@app.post("/api/bili/login/start")
def api_bili_login_start():
    import qrcode
    import base64
    gen = vp._bili_get(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    if gen.get("code") != 0:
        return jsonify(error="生成二维码失败"), 500
    LOGIN["key"] = gen["data"]["qrcode_key"]
    LOGIN["cookie"] = ""
    img = qrcode.make(gen["data"]["url"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return jsonify(qr=f"data:image/png;base64,{data}")


@app.get("/api/bili/login/poll")
def api_bili_login_poll():
    import http.cookiejar
    from urllib.request import build_opener, HTTPCookieProcessor, Request
    import json as _json
    key = LOGIN.get("key")
    if not key:
        return jsonify(state="none")
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
           f"?qrcode_key={key}")
    r = Request(url, headers={"User-Agent": vp.UA,
                              "Referer": "https://www.bilibili.com/"})
    with opener.open(r, timeout=20) as resp:
        data = _json.load(resp)["data"]
    code = data.get("code")
    if code == 0:
        want = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")
        cookie = "; ".join(f"{c.name}={c.value}" for c in jar if c.name in want)
        if cookie:
            vp.bili_save_cookie(cookie)
        LOGIN["key"] = ""
        ok, name = vp.bili_login_status()
        return jsonify(state="ok", name=name)
    return jsonify(state={86038: "expired", 86090: "scanned"}.get(code, "waiting"))


@app.post("/api/bili/logout")
def api_bili_logout():
    vp.bili_logout()
    return jsonify(ok=True)


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    import webbrowser
    port = int(os.environ.get("VP_PORT") or 8731)
    url = f"http://127.0.0.1:{port}"
    print(f"网页视频提取已启动： {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)
