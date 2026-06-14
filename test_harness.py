"""自动化端到端测试：解析→浏览器可达性→系统播放器→下载→音频完整性→速度。

对每个视频选一条流（短视频取高清，长视频取较低分辨率省带宽，但全时长），
验证 app 真实使用的解析(parser)与下载(downloader)代码路径。
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request

import parser as vp
import downloader

OUT = "/tmp/vptest"
os.makedirs(OUT, exist_ok=True)
YTDLP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "yt-dlp")
ARIA2 = shutil.which("aria2c")

# (平台, 标签, url, 是否长视频)
VIDEOS = [
    ("YouTube", "短-Me at the zoo", "https://www.youtube.com/watch?v=jNQXAC9IVRw", False),
    ("YouTube", "长-Vivaldi四季42min", "https://www.youtube.com/watch?v=GRxofEmo3HA", True),
    ("B站", "短-BV1d9 3.5min", "https://www.bilibili.com/video/BV1d9E16BEp3", False),
    ("B站", "长-BV17GE 51min", "https://www.bilibili.com/video/BV17GEi61EZK", True),
    ("抖音", "短-76s", "https://www.douyin.com/video/7650087444679193974", False),
    ("抖音", "较长-9min", "https://www.douyin.com/video/7641952986054380827", True),
    ("archive", "短-About Bananas 11min", "https://archive.org/details/AboutBan1935", False),
    ("archive", "长-活死人之夜 95min", "https://archive.org/details/night-of-the-living-dead-1968_202110", True),
]

log_lines = []
def L(m):
    print(m, flush=True)
    log_lines.append(m)


def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def last_audio_pts(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "a",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    ts = [float(x) for x in r.stdout.split() if x.strip().replace('.', '').isdigit()]
    return max(ts) if ts else 0.0


def browser_reachable(s):
    """模拟浏览器：不带 Referer 请求流地址，看是否可访问。"""
    try:
        req = urllib.request.Request(s.url, headers={"User-Agent": vp.UA},
                                     method="GET")
        req.add_header("Range", "bytes=0-1023")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", "")
    except Exception as e:  # noqa: BLE001
        return getattr(e, "code", "ERR"), str(e)[:40]


def sysplayer_ok(s):
    """mpv 用真实播放参数解码前 6 秒，确认视频+音频都能打开。"""
    cmd = ["mpv", "--vo=null", "--ao=null", "--no-config", "--length=6",
           "--hwdec=no"]
    if s.referer:
        cmd.append(f"--referrer={s.referer}")
    if s.audio_url:
        cmd.append(f"--audio-file={s.audio_url}")
    cmd.append(s.url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        has_v = "VO:" in out or "Video --vid" in out or "(h264" in out or "AV:" in out
        has_a = "AO:" in out or "Audio --aid" in out
        return has_v, has_a, ""
    except Exception as e:  # noqa: BLE001
        return False, False, str(e)[:50]


def pick_stream(res, is_long):
    """短视频取最高清；长视频取一个较低分辨率(省带宽)但仍是音视频流。"""
    vids = [s for s in res.streams if s.vcodec not in ("", "none")]
    if not vids:
        return res.streams[0] if res.streams else None
    if is_long:
        # 选高度<=480 里最高的，没有就选最低码率
        def h(s):
            try:
                return int(s.resolution.split("x")[1]) if "x" in s.resolution else 9999
            except (ValueError, IndexError):
                return 9999
        low = [s for s in vids if h(s) <= 480]
        return max(low, key=lambda s: s.tbr) if low else min(vids, key=lambda s: s.tbr)
    return max(vids, key=lambda s: s.tbr)


def test_one(platform, label, url, is_long):
    R = {"platform": platform, "label": label}
    L(f"\n{'='*60}\n【{platform}】{label}\n{url}")
    # 1) 解析
    t = time.time()
    try:
        res = vp.parse(url, log=lambda m: None)
    except Exception as e:  # noqa: BLE001
        R["parse"] = f"解析异常: {e}"
        return R
    if not res.streams:
        R["parse"] = f"无流: {res.error}"
        L("  解析失败: " + (res.error or ""))
        return R
    R["parse"] = f"OK {len(res.streams)}流 ({time.time()-t:.1f}s)"
    R["title"] = res.title[:30]
    s = pick_stream(res, is_long)
    R["pick"] = f"{s.note or s.resolution} {s.vcodec[:8]} 音视频={s.kind} 合并音频={bool(s.audio_url)} src={s.source}"
    L(f"  解析: {R['parse']} | 选流: {R['pick']}")

    # 2) 浏览器可达
    code, ctype = browser_reachable(s)
    R["browser"] = f"{code} {ctype[:30]}"
    L(f"  浏览器可达(无Referer): {R['browser']}")

    # 3) 系统播放器
    hv, ha, err = sysplayer_ok(s)
    R["sysplayer"] = f"视频={'✓' if hv else '✗'} 音频={'✓' if ha else '✗'} {err}"
    L(f"  系统播放器: {R['sysplayer']}")

    # 4) 下载 + 校验
    out = os.path.join(OUT, f"{platform}_{'long' if is_long else 'short'}.mp4")
    if os.path.exists(out):
        os.remove(out)
    t = time.time()
    ok = downloader.download(s, res.webpage_url or url, out,
                             log=lambda m: None, ytdlp=YTDLP, aria2=ARIA2)
    dt = time.time() - t
    if not ok or not os.path.exists(out):
        R["download"] = "✗ 下载失败"
        L("  下载: ✗ 失败")
        return R
    sz = os.path.getsize(out) / 1024 / 1024
    info = ffprobe(out)
    streams = info.get("streams", [])
    has_v = any(st["codec_type"] == "video" for st in streams)
    has_a = any(st["codec_type"] == "audio" for st in streams)
    dur = float(info.get("format", {}).get("duration", 0))
    a_pts = last_audio_pts(out) if has_a else 0
    cov = (a_pts / dur * 100) if dur else 0
    decode_ok = downloader.media_ok(out)
    R["download"] = (f"{sz:.0f}MB / {dt:.0f}s = {sz/dt:.2f}MB/s | "
                     f"视频{'✓' if has_v else '✗'}音频{'✓' if has_a else '✗'} | "
                     f"时长{dur:.0f}s 音频覆盖到{a_pts:.0f}s({cov:.0f}%) | "
                     f"完整解码{'✓' if decode_ok else '✗损坏'}")
    L(f"  下载: {R['download']}")
    return R


def main():
    results = []
    for v in VIDEOS:
        try:
            results.append(test_one(*v))
        except Exception as e:  # noqa: BLE001
            L(f"  测试异常: {e}")
            results.append({"platform": v[0], "label": v[1], "error": str(e)})
    # 汇总
    L("\n\n" + "#"*60 + "\n# 汇总报告\n" + "#"*60)
    for R in results:
        L(f"\n[{R.get('platform')}] {R.get('label')}")
        for k in ("parse", "pick", "browser", "sysplayer", "download", "error"):
            if k in R:
                L(f"    {k:10}: {R[k]}")
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    L(f"\n报告已存: {OUT}/report.txt")


if __name__ == "__main__":
    main()
