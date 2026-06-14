let streams = [];
let sel = -1;
let loginTimer = null;

const $ = (id) => document.getElementById(id);
function log(msg) {
  const el = $("log");
  el.textContent += msg + "\n";
  el.scrollTop = el.scrollHeight;
}
function setStatus(m) { $("status").textContent = m; }

$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") doParse(); });

async function pasteUrl() {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { $("url").value = t.trim(); doParse(); }
  } catch (e) {
    $("url").focus();
    setStatus("无法自动读取剪贴板，请按 Ctrl+V 粘贴后回车");
  }
}

function showPlaceholder(show) {
  $("placeholder").style.display = show ? "block" : "none";
}

async function doParse() {
  const url = $("url").value.trim();
  if (!url) return;
  $("parseBtn").disabled = true;
  $("parseBtn").textContent = "解析中…";
  $("log").textContent = "";
  $("player").pause(); $("player").removeAttribute("src"); $("player").load();
  showPlaceholder(true);
  try {
    const r = await fetch("/api/parse", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const d = await r.json();
    (d.logs || []).forEach(log);
    if (d.error && !(d.streams && d.streams.length)) {
      setStatus("解析失败"); alert("解析失败：" + d.error); return;
    }
    streams = d.streams;
    $("title").textContent = d.title || "(无标题)";
    if (d.duration) { $("durChip").style.display = ""; $("durChip").textContent = "时长 " + d.duration; }
    renderRows();
    $("result").style.display = "";
    setStatus(`完成，共 ${d.count} 个视频流`);
  } catch (e) {
    alert("出错：" + e);
  } finally {
    $("parseBtn").disabled = false;
    $("parseBtn").textContent = "解析";
  }
}

function renderRows() {
  const tb = $("rows");
  tb.innerHTML = "";
  streams.forEach((s) => {
    const tr = document.createElement("tr");
    tr.className = "pick";
    tr.onclick = () => selectRow(s.i);
    tr.ondblclick = () => selectAndPlay(s.i);
    tr.title = "双击直接播放";
    tr.id = "row" + s.i;
    const tags = [];
    if (s.needs_referer) tags.push('<span class="tag">防盗链</span>');
    tr.innerHTML = `<td>${s.quality} ${tags.join("")}</td><td>${s.codec}</td>
      <td>${s.kind}</td><td>${s.tbr || "-"}</td><td>${s.size}</td><td>${s.source}</td>`;
    tb.appendChild(tr);
  });
  if (streams.length) selectRow(0);
}

function selectRow(i) {
  sel = i;
  document.querySelectorAll("#rows tr").forEach((tr) => tr.classList.remove("sel"));
  const tr = $("row" + i); if (tr) tr.classList.add("sel");
}

function playSel() {
  if (sel < 0) return;
  const s = streams.find((x) => x.i === sel);
  const v = $("player");
  v.pause(); v.removeAttribute("src"); v.load();
  showPlaceholder(false);
  $("stage").scrollIntoView({ behavior: "smooth", block: "center" });

  // 合并流(单文件)：直接代理播放，秒开、可拖动
  if (!s.split) {
    v.src = "/api/play/" + sel;
    v.play().catch(() => {});
    setStatus("在线播放中…");
    return;
  }
  // 分离音视频：后端先下载合并成普通 mp4，再播放（最可靠，可拖动）
  setStatus("缓冲中（下载并合并，清晰度越高越久；想快可选低清）…");
  previewSplit();
}

// 双击某一行 = 选中并直接播放
function selectAndPlay(i) { selectRow(i); playSel(); }

async function previewSplit() {
  const d = await (await fetch("/api/preview/" + sel, { method: "POST" })).json();
  if (d.error) { alert(d.error); return; }
  pollPreview(d.job);
}

async function pollPreview(job) {
  const d = await (await fetch("/api/download/status/" + job)).json();
  if (d.state === "running") {
    if (d.line) setStatus("缓冲中 — " + d.line);
    setTimeout(() => pollPreview(job), 1000);
    return;
  }
  if (d.state === "done") {
    const v = $("player");
    v.src = "/api/preview/file/" + job;
    v.play().catch(() => {});
    setStatus("▶ 播放中（已缓冲，可自由拖动）");
  } else {
    setStatus("✗ 缓冲失败");
    alert("在线播放缓冲失败，详见日志");
  }
}

async function downloadSel() {
  if (sel < 0) return;
  setStatus("准备下载…");
  const r = await fetch("/api/download/" + sel, { method: "POST" });
  const d = await r.json();
  if (d.error) { alert(d.error); return; }
  pollDownload(d.job);
}

async function pollDownload(job) {
  const r = await fetch("/api/download/status/" + job);
  const d = await r.json();
  if (d.line) setStatus((d.status || "下载中") + " — " + d.line);
  else setStatus(d.status || "下载中…");
  if (d.state === "running") { setTimeout(() => pollDownload(job), 1000); return; }
  if (d.state === "done") {
    setStatus("✓ 下载完成，正在保存到浏览器…");
    window.location = "/api/download/file/" + job;
  } else {
    setStatus("✗ 下载失败");
    alert("下载失败，详见日志");
  }
}

function copySel() {
  if (sel < 0) return;
  // 直接取播放代理地址（真实地址含签名很长，且防盗链不可直接用）
  navigator.clipboard.writeText(location.origin + "/api/play/" + sel)
    .then(() => setStatus("已复制后端播放地址（可在本机播放器打开）"));
}

// ---- B 站登录 ----
async function refreshBili() {
  try {
    const d = await (await fetch("/api/bili/status")).json();
    $("biliChip").textContent = "B站: " + d.name;
    $("biliChip").className = "chip" + (d.logged_in ? " ok" : "");
    $("biliBtn").textContent = d.logged_in ? "重新登录" : "登录B站";
  } catch (e) {}
}
async function biliLogin() {
  $("loginModal").style.display = "flex";
  $("loginTip").textContent = "加载二维码…";
  const d = await (await fetch("/api/bili/login/start", { method: "POST" })).json();
  if (d.error) { $("loginTip").textContent = d.error; return; }
  $("qrImg").src = d.qr;
  $("loginTip").textContent = "请用手机 B 站 App 扫码";
  loginTimer = setInterval(pollLogin, 2000);
}
async function pollLogin() {
  const d = await (await fetch("/api/bili/login/poll")).json();
  if (d.state === "ok") {
    clearInterval(loginTimer);
    $("loginTip").textContent = "✓ 登录成功：" + d.name;
    setTimeout(closeLogin, 1000);
    refreshBili();
  } else if (d.state === "scanned") {
    $("loginTip").textContent = "已扫码，请在手机上确认…";
  } else if (d.state === "expired") {
    clearInterval(loginTimer);
    $("loginTip").textContent = "二维码已过期，请重新点登录";
  }
}
function closeLogin() {
  $("loginModal").style.display = "none";
  if (loginTimer) clearInterval(loginTimer);
}

refreshBili();
