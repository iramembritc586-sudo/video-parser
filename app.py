"""网页视频地址提取 - 桌面 GUI。

输入网页地址，提取其中真实的视频流地址（m3u8 / mp4 等），
可复制地址、用系统播放器打开，或直接下载到本地。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import parser as vp
import downloader

HERE = os.path.dirname(os.path.abspath(__file__))


def _venv_tool(name):
    """跨平台定位 venv 可执行文件（Windows: Scripts/*.exe；Unix: bin/*）。"""
    for sub in ("Scripts", "bin"):
        for ext in (".exe", ""):
            p = os.path.join(HERE, ".venv", sub, name + ext)
            if os.path.exists(p):
                return p
    return shutil.which(name) or name


YTDLP = _venv_tool("yt-dlp")
ARIA2 = shutil.which("aria2c")  # 多线程下载器，没装则为 None（退回单连接）


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("网页视频地址提取")
        self.geometry("960x620")
        self.minsize(760, 480)

        self._q: queue.Queue = queue.Queue()
        self._streams: list[vp.Stream] = []
        self._busy = False

        self._login_win = None
        self._build_ui()
        self.after(80, self._drain)
        self._refresh_login_status()  # 启动时检查已保存的登录态

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="网页地址:").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.url_entry.bind("<Return>", lambda e: self._on_parse())
        self.parse_btn = ttk.Button(top, text="解析", command=self._on_parse)
        self.parse_btn.pack(side="left")
        ttk.Button(top, text="粘贴", command=self._paste).pack(side="left", padx=(6, 0))

        info = ttk.Frame(self)
        info.pack(fill="x", padx=8)
        self.login_btn = ttk.Button(info, text="登录B站", width=10,
                                    command=self._bili_login)
        self.login_btn.pack(side="left")
        self.bili_status = tk.StringVar(value="B站: 未登录")
        ttk.Label(info, textvariable=self.bili_status,
                  foreground="#888").pack(side="left", padx=(6, 0))
        self.title_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.title_var, foreground="#235",
                  font=("", 10, "bold")).pack(side="left", padx=(16, 0))

        # 结果表格
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("res", "ext", "kind", "tbr", "size", "proto", "src")
        heads = {"res": "清晰度", "ext": "编码/格式", "kind": "类型", "tbr": "码率kbps",
                 "size": "大小", "proto": "协议", "src": "来源"}
        widths = {"res": 160, "ext": 70, "kind": 80, "tbr": 90, "size": 90,
                  "proto": 90, "src": 80}
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], anchor="center")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._copy_url())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected_url())

        # 选中地址栏
        urlbar = ttk.Frame(self)
        urlbar.pack(fill="x", padx=8)
        ttk.Label(urlbar, text="视频地址:").pack(side="left")
        self.sel_var = tk.StringVar()
        sel = ttk.Entry(urlbar, textvariable=self.sel_var, state="readonly")
        sel.pack(side="left", fill="x", expand=True, padx=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="复制地址", command=self._copy_url).pack(side="left")
        ttk.Button(btns, text="浏览器打开", command=self._open_browser).pack(side="left", padx=6)
        ttk.Button(btns, text="系统播放器播放", command=self._play).pack(side="left")
        ttk.Button(btns, text="下载选中", command=self._download).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(btns, textvariable=self.status_var, foreground="#666").pack(side="right")

        # 日志
        logf = ttk.LabelFrame(self, text="日志")
        logf.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log = tk.Text(logf, height=7, wrap="word", state="disabled",
                           bg="#1e1e1e", fg="#d4d4d4", font=("monospace", 9))
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")

    # ---------- 线程 / 队列 ----------
    def _logmsg(self, msg: str):
        self._q.put(("log", msg))

    def _drain(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "result":
                    self._render(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self._set_busy(False)
                elif kind == "bili":
                    ok, name = payload
                    self.bili_status.set(f"B站: {name}")
                    self.login_btn.configure(text="重新登录" if ok else "登录B站")
                elif kind == "qr":
                    from PIL import ImageTk
                    self._qr_img = ImageTk.PhotoImage(payload)  # 防 GC
                    if getattr(self, "_qr_label", None):
                        self._qr_label.configure(image=self._qr_img)
                elif kind == "qrtip":
                    if getattr(self, "_qr_tip", None):
                        self._qr_tip.set(payload)
                elif kind == "login_done":
                    if getattr(self, "_login_win", None):
                        self._login_win.destroy()
                        self._login_win = None
                    if payload:
                        messagebox.showinfo("登录成功",
                                            "已保存登录态，以后启动会自动登录。")
                    self._refresh_login_status()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _append_log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.parse_btn.configure(state="disabled" if busy else "normal",
                                 text="解析中…" if busy else "解析")

    # ---------- 动作 ----------
    def _paste(self):
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    # ---------- B 站登录 ----------
    def _refresh_login_status(self):
        """后台检查本地登录态，更新状态显示。"""
        def chk():
            ok, name = vp.bili_login_status()
            self._q.put(("bili", (ok, name)))
        threading.Thread(target=chk, daemon=True).start()

    def _bili_login(self):
        if getattr(self, "_login_win", None):
            return
        ok, name = vp.bili_login_status()
        if ok and not messagebox.askyesno(
                "已登录", f"当前已登录：{name}\n是否重新登录 / 切换账号？"):
            return
        self._open_login_window()

    def _open_login_window(self):
        win = tk.Toplevel(self)
        win.title("扫码登录 B 站")
        win.geometry("320x400")
        win.resizable(False, False)
        self._login_win = win
        self._login_stop = False
        ttk.Label(win, text="请用手机 B 站 App 扫码登录",
                  font=("", 11)).pack(pady=10)
        self._qr_label = ttk.Label(win)
        self._qr_label.pack(pady=4)
        self._qr_tip = tk.StringVar(value="正在生成二维码…")
        ttk.Label(win, textvariable=self._qr_tip, foreground="#666").pack(pady=6)

        def on_close():
            self._login_stop = True
            self._login_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        threading.Thread(target=self._login_work, daemon=True).start()

    def _login_work(self):
        def show_qr(url):
            import qrcode
            img = qrcode.make(url).resize((240, 240))
            self._q.put(("qr", img))
        def tip(m):
            self._logmsg(m)
            self._q.put(("qrtip", m))
        try:
            cookie = vp.bili_qr_login(show_qr, lambda: self._login_stop, log=tip)
            self._q.put(("login_done", bool(cookie)))
        except Exception as e:  # noqa: BLE001
            self._logmsg(f"登录出错：{e}")
            self._q.put(("login_done", False))

    def _on_parse(self):
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("提示", "请输入网页地址")
            return
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._streams = []
        self.sel_var.set("")
        self.title_var.set("")
        self._set_busy(True)
        self._q.put(("status", "解析中…"))
        threading.Thread(target=self._work, args=(url,), daemon=True).start()

    def _work(self, url: str):
        try:
            # B 站自动使用已保存的登录态（parse 内部会 bili_load_cookie）
            res = vp.parse(url, log=self._logmsg)
            self._q.put(("result", res))
        except Exception as e:  # noqa: BLE001
            self._logmsg(f"✗ 出错：{e}")
            self._q.put(("result", vp.ParseResult(error=str(e))))
        finally:
            self._q.put(("done", None))

    def _render(self, res: vp.ParseResult):
        # 记录解析后的规范网址，下载时用它（原始输入可能是 yt-dlp 不认的弹窗地址）
        self._page_url = res.webpage_url or self.url_var.get().strip()
        if res.error and not res.streams:
            self.status_var.set("解析失败")
            messagebox.showwarning("解析失败", res.error)
            return
        t = res.title or "(无标题)"
        if res.duration_human:
            t += f"   时长 {res.duration_human}"
        if res.entries:
            t += f"   [播放列表 {len(res.entries)} 项]"
        self.title_var.set(t)
        self._streams = res.streams
        def codec_name(vc: str) -> str:
            if vc.startswith("avc"):
                return "H.264"
            if vc.startswith(("hvc", "hev")):
                return "H.265"
            if "av01" in vc:
                return "AV1"
            return vc.split(".")[0] if vc and vc != "none" else "-"

        for idx, s in enumerate(res.streams):
            # 清晰度优先显示档位名（1080p/720p），括号附分辨率
            if s.note and s.resolution:
                quality = f"{s.note} ({s.resolution})"
            else:
                quality = s.note or s.resolution or "-"
            self.tree.insert("", "end", iid=str(idx), values=(
                quality,
                codec_name(s.vcodec) if s.vcodec != "none" else (s.ext or "-"),
                s.kind,
                f"{s.tbr:.0f}" if s.tbr else "-",
                s.size_human,
                s.protocol or "-",
                s.source,
            ))
        self.status_var.set(f"完成，共 {len(res.streams)} 个视频流")
        if res.streams:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self._show_selected_url()

    def _selected(self) -> vp.Stream | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._streams[int(sel[0])]

    def _show_selected_url(self):
        s = self._selected()
        if s:
            self.sel_var.set(s.url)

    def _copy_url(self):
        s = self._selected()
        if not s:
            return
        self.clipboard_clear()
        self.clipboard_append(s.url)
        self.status_var.set("已复制地址到剪贴板")

    def _open_browser(self):
        s = self._selected()
        if not s:
            return
        if s.referer:  # 防盗链地址，浏览器直接打开必然 403
            messagebox.showwarning(
                "无法用浏览器打开",
                "该地址有防盗链保护（如 B 站），浏览器直接访问会 403。\n\n"
                "请改用「系统播放器播放」预览，或「下载选中」保存到本地。")
            return
        if s.audio_url:  # 纯视频流(音频另存)，浏览器只放视频会没声音
            if not messagebox.askyesno(
                    "浏览器里没有声音",
                    "该清晰度的视频和音频是分开的，浏览器只能打开视频画面（没有声音）。\n\n"
                    "想听声音请用「系统播放器播放」或「下载选中」。\n\n"
                    "仍要在浏览器打开（仅画面）吗？"):
                return
        webbrowser.open(s.url)

    def _play(self):
        s = self._selected()
        if not s:
            return
        # 防抖：3 秒内重复点击忽略，避免开出多个播放器窗口
        now = time.monotonic()
        if now - getattr(self, "_last_play", 0) < 3:
            self.status_var.set("播放器启动中，请稍候…（勿重复点击）")
            return
        self._last_play = now
        player = shutil.which("mpv") or shutil.which("vlc")
        if not player:
            messagebox.showinfo(
                "未安装播放器",
                "未检测到 mpv 或 vlc。\n防盗链地址无法用浏览器播放，"
                "建议安装 mpv：sudo apt install mpv\n或直接用「下载选中」保存。")
            return
        try:
            is_mpv = "mpv" in player
            args = [player]
            if is_mpv:
                # force-window=immediate：窗口立刻弹出(先黑屏)，不让人误以为没反应
                # 硬解 + 磁盘缓存：下过的部分存盘，往回拖/重复拖秒回不重载
                args += ["--force-window=immediate",
                         "--title=正在加载… - 网页视频",
                         "--hwdec=auto-safe",
                         "--cache=yes", "--cache-on-disk=yes",
                         "--cache-secs=300",
                         "--demuxer-max-bytes=1GiB",
                         "--demuxer-max-back-bytes=1GiB",
                         "--demuxer-readahead-secs=60",
                         "--hr-seek=yes", "--force-seekable=yes"]
            if s.referer:  # B 站等 CDN 需要 Referer
                args += [f"--referrer={s.referer}"] if is_mpv else []
            if s.audio_url and is_mpv:  # DASH 分离音轨：挂上外部音频一起放
                args += [f"--audio-file={s.audio_url}"]
            args += [s.url]
            if s.referer and not is_mpv:  # vlc 的 referer 写法
                args += [f":http-referrer={s.referer}", ":network-caching=60000"]
            subprocess.Popen(args)
            self.status_var.set("播放器窗口已弹出，缓冲中…首帧需几秒")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("播放失败", str(e))

    def _download(self):
        s = self._selected()
        if not s:
            messagebox.showinfo("提示", "请先在列表里选择一个视频流")
            return
        if self._busy:
            return
        default = (self.title_var.get().split("  ")[0] or "video").strip()[:60]
        path = filedialog.asksaveasfilename(
            title="保存视频", initialfile=f"{default}.{s.ext or 'mp4'}",
            defaultextension=f".{s.ext or 'mp4'}")
        if not path:
            return
        # 用解析后的规范网址（原始输入可能是 yt-dlp 不认的弹窗地址）
        page = getattr(self, "_page_url", "") or self.url_var.get().strip()
        self._set_busy(True)
        self.status_var.set("下载中…")
        threading.Thread(target=self._dl_work, args=(s, page, path),
                         daemon=True).start()

    def _dl_work(self, s: vp.Stream, page: str, path: str):
        try:
            ok = downloader.download(
                s, page, path, log=self._logmsg, ytdlp=YTDLP, aria2=ARIA2,
                status=lambda m: self._q.put(("status", m)))
            if ok:
                self._logmsg(f"✓ 下载完成：{path}")
                self._q.put(("status", "下载完成"))
            else:
                self._logmsg("✗ 下载失败")
                self._q.put(("status", "下载失败"))
        except Exception as e:  # noqa: BLE001
            self._logmsg(f"✗ 下载出错：{e}")
            self._q.put(("status", "下载失败"))
        finally:
            self._q.put(("done", None))


if __name__ == "__main__":
    App().mainloop()
