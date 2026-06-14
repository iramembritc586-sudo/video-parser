"""下载逻辑（app 与测试共用，保证一致）。

支持：
- yt-dlp 来源：按 format 下载，纯视频流自动 +bestaudio 合并；可选 aria2c 加速。
- B 站/直链 DASH：aria2c 多线程下视频+音频，ffmpeg 合并；音频解码校验，损坏则单连接重下。
- 无 aria2c 时退回 ffmpeg 单连接。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import parser as vp


def _run(cmd, log) -> int:
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if line:
            log(line)
    proc.wait()
    return proc.returncode


def _aria2(aria2, url, out_dir, out_name, referer, log, conns=16) -> bool:
    cmd = [aria2, f"-x{conns}", f"-s{conns}", "-k1M", "--summary-interval=1",
           "--console-log-level=warn", "--auto-file-renaming=false",
           "--allow-overwrite=true", f"--user-agent={vp.UA}"]
    if referer:
        cmd += [f"--referer={referer}"]
    cmd += ["-d", out_dir, "-o", out_name, url]
    return _run(cmd, log) == 0


def media_ok(path: str) -> bool:
    """完整解码校验：能从头到尾解出且无错误（可抓出中段损坏/截断）。"""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-xerror",
                            "-i", path, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0 and not r.stderr.strip()
    except Exception:  # noqa: BLE001
        return False


def _fetch_audio(aria2, url, tmp, referer, log) -> bool:
    ap = os.path.join(tmp, "a.m4a")
    if _aria2(aria2, url, tmp, "a.m4a", referer, log) and media_ok(ap):
        return True
    log("⚠ 音频多线程下载校验未过，改用单连接重下…")
    return _aria2(aria2, url, tmp, "a.m4a", referer, log, conns=1) and media_ok(ap)


def download(s: vp.Stream, page: str, path: str, log=lambda m: None,
             ytdlp="yt-dlp", aria2=None, status=lambda m: None) -> bool:
    """下载一条流到 path，返回是否成功。"""
    tmp = tempfile.mkdtemp(prefix="vp_dl_")
    try:
        if s.source == "yt-dlp" and s.format_id:
            fmt = s.format_id + ("+bestaudio/bestaudio" if s.audio_url else "")
            cmd = [ytdlp, "-f", fmt, "--merge-output-format", "mp4", "-o", path]
            if aria2:
                cmd += ["--downloader", "aria2c",
                        "--downloader-args", "aria2c:-x16 -s16 -k1M"]
            cmd += [page]
            return _run(cmd, log) == 0
        if aria2:
            status("多线程下载中…")
            if not _aria2(aria2, s.url, tmp, "v.mp4", s.referer, log):
                return False
            if s.audio_url:
                if not _fetch_audio(aria2, s.audio_url, tmp, s.referer, log):
                    return False
                status("合并中…")
                return _run(["ffmpeg", "-y", "-i", os.path.join(tmp, "v.mp4"),
                             "-i", os.path.join(tmp, "a.m4a"),
                             "-map", "0:v", "-map", "1:a", "-c", "copy",
                             path], log) == 0
            shutil.move(os.path.join(tmp, "v.mp4"), path)
            return True
        # 无 aria2c：ffmpeg 单连接
        cmd = ["ffmpeg", "-y"]
        hdr = f"Referer: {s.referer}\r\nUser-Agent: {vp.UA}\r\n" if s.referer else ""
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-i", s.url]
        if s.audio_url:
            if hdr:
                cmd += ["-headers", hdr]
            cmd += ["-i", s.audio_url, "-map", "0:v", "-map", "1:a"]
        cmd += ["-c", "copy"]
        if s.protocol in ("hls", "m3u8") or s.ext in ("ts", "m3u8"):
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += [path]
        return _run(cmd, log) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
