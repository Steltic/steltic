"use strict";
const $ = (id) => document.getElementById(id);
const show = (el, on) => el.classList.toggle("hidden", !on);
let attachedImages = [];   // {name,type,data_url,size} staged image attachments (sent to the model)
let backupBlob = null, backupUrl = null, backupBuilding = null, reportUrl = null, modelUrl = null;   // in-browser copy of the unified zip (serves Download/View AND restores Continue)
let reasonLine = null;     // streaming line for the Model reasoning box

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  return r;
}
async function jpost(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(body || {}) });
}

// ---------------- bootstrap ----------------
async function boot() {
  let me = {};
  try { me = await (await api("/api/me")).json(); } catch (_) {}
  if (me.executor) $("execBadge").textContent = "sandbox: " + me.executor;
  if (me.has_creds) {
    $("model").value = me.model || "";
    $("baseUrl").value = me.base_url || "";
  } else {
    setSettings(true);  // first thing: ask for the LLM connection
  }
  if (me.reasoning) $("reasoning").value = me.reasoning;
  if (me.max_tokens) $("maxTokens").value = me.max_tokens;
  if (me.provider) $("provider").value = me.provider;
}

function setSettings(on) { show($("settings"), on); $("settingsBtn").classList.toggle("active", on); }
$("settingsBtn").addEventListener("click", () => setSettings($("settings").classList.contains("hidden")));

// ---------------- creds ----------------
$("saveCreds").addEventListener("click", async () => {
  const body = { base_url: $("baseUrl").value, api_key: $("apiKey").value, model: $("model").value,
                 reasoning: $("reasoning").value, max_tokens: parseInt($("maxTokens").value) || 32000,
                 provider: $("provider").value.trim() };
  const r = await jpost("/api/creds", body);
  const msg = $("credsMsg");
  if (r.ok) { msg.textContent = "saved ✓"; setTimeout(() => setSettings(false), 600); }
  else { msg.textContent = (await r.json().catch(() => ({}))).detail || "error"; }
});

// ---------------- run ----------------
const logEl = () => $("log");
function logLine(cls, text) {
  const d = document.createElement("div");
  if (cls) d.className = cls;
  d.textContent = text;
  logEl().appendChild(d);
  logEl().scrollTop = logEl().scrollHeight;
  return d;
}
let tokenLine = null;
// small looping mark shown inline (≈1 line tall) at the end of whichever line is actively streaming
function _gifFor(lineEl) {
  let g = lineEl.querySelector(".streamgif");
  if (!g) { g = document.createElement("img"); g.className = "streamgif"; g.src = "/static/Steltic-mark-loop.gif"; g.alt = ""; }
  lineEl.appendChild(g);   // (re)place at the end, after the latest text
}
function appendToken(t) {
  if (!tokenLine) { tokenLine = logLine("tok", ""); tokenLine._txt = document.createElement("span"); tokenLine.appendChild(tokenLine._txt); }
  tokenLine._txt.textContent += t;
  _gifFor(tokenLine);
  logEl().scrollTop = logEl().scrollHeight;
}
const reasonEl = () => $("reason");
const REASON_CAP = 120000;   // chars; drop the oldest reasoning when the box grows past this (keeps the DOM light + scrolling)
function appendReason(t) {
  const el = reasonEl();
  if (!reasonLine) { reasonLine = document.createElement("div"); reasonLine.className = "rtok"; reasonLine._txt = document.createElement("span"); reasonLine.appendChild(reasonLine._txt); el.appendChild(reasonLine); }
  reasonLine._txt.textContent += t;
  _gifFor(reasonLine);
  while (el.textContent.length > REASON_CAP && el.firstChild && el.firstChild !== reasonLine) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}
const fmtMs = (ms) => ms < 1000 ? ms + "ms" : (ms / 1000).toFixed(1) + "s";

// ---------------- agent activity lights ----------------
function setLights(keys) {
  const on = new Set(keys || []);
  document.querySelectorAll("#activity .light").forEach((el) => el.classList.toggle("on", on.has(el.dataset.k)));
}
function updateLights(ev) {
  switch (ev.type) {
    case "token": case "reasoning": setLights(["thinking"]); break;
    case "tool": {
      const n = ev.name;
      if (n === "run_python") {
        const k = ["python"];
        if (/opensees|ops\.|pipeline|design|report|preview|build|analyz|eigen|recorder|modal/i.test((ev.code || "") + " " + (ev.title || ""))) k.push("opensees");
        setLights(k);
      } else if (n === "search_engineering_standards") { setLights(["rag"]); }
      break;
    }
    case "tool_result": setLights(["thinking"]); break;
    case "assistant": case "done": case "error": case "paused": setLights([]); break;
  }
}

function handleEvent(ev, building) {
  if (ev.type !== "token") { if (tokenLine) { const g = tokenLine.querySelector(".streamgif"); if (g) g.remove(); } tokenLine = null; }
  if (ev.type !== "reasoning") { if (reasonLine) { const g = reasonLine.querySelector(".streamgif"); if (g) g.remove(); } reasonLine = null; }
  updateLights(ev);
  switch (ev.type) {
    case "status":      logLine("status", "· " + ev.text); break;
    case "token":       appendToken(ev.text); break;
    case "tool":        logLine("tool", `▶ step ${ev.step} · ${ev.title || ev.name}`); break;
    case "tool_result": logLine("res", "↳ " + ev.summary + (ev.ms ? "  (" + fmtMs(ev.ms) + ")" : "")); break;
    case "milestone":   logLine("milestone", "▸ " + ev.text); break;
    case "reasoning":   appendReason(ev.text); break;
    case "assistant":   logLine("assistant", ev.text); break;
    case "error":       logLine("err", "✖ " + ev.text); break;
    case "paused": {
      logLine("paused", "⏸ paused — " + (ev.reason || "loop guard"));
      if (ev.detail) logLine("paused", "   " + ev.detail);
      if (stopWaitTimer) { clearTimeout(stopWaitTimer); stopWaitTimer = null; }
      $("runStatus").textContent = /stopped/.test(ev.reason || "")
        ? "stopped — your files arrived below (Download); click Continue to resume"
        : "paused — review the log, type a fix or instruction, then click Continue";
      show($("resumeBtn"), true);
      // the 'bundle' SSE event (arrives next) captures the paused state to the browser for Download + restore
      break;
    }
    case "usage": {
      const f = (n) => Number(n || 0).toLocaleString();
      $("tokensLast").textContent = `last call  input ${f(ev.last_in)} · output ${f(ev.last_out)}`;
      $("tokensCum").textContent = `cumulative  input ${f(ev.cum_in)} · output ${f(ev.cum_out)}`;
      show($("tokensSep"), true); show($("tokensInfoCtx"), true); show($("tokensInfo"), true);
      break;
    }
    case "done": {
      const a = $("reportLink");
      a.href = `/api/report/${encodeURIComponent(building)}/report.html`;
      show(a, true);
      $("modelLink").href = `/api/report/${encodeURIComponent(building)}/viewer_3d.html`;
      show($("modelLink"), true);
      $("downloadLink").href = `/api/download/${encodeURIComponent(building)}`;
      show($("downloadLink"), true);
      // the 'bundle' SSE event (arrives next, on this same connection) replaces these with an in-browser copy
      $("runStatus").textContent = "done — type a change above and click Continue to iterate";
      $("brief").value = "";
      $("brief").placeholder = "Follow-up (continues THIS design with full context): e.g. \"make the exterior columns W24X146 and rerun\", \"pin the interior beam-to-column joints\", or \"run an optimisation pass to lighten the members\". Then click Continue.";
      $("briefFileName").textContent = "";
      attachedImages = []; renderFileList();
      break;
    }
    case "bundle": {
      // The server shipped the whole design (report + resumable state) over THIS connection. Capture it
      // so Download / View / Continue all work from the browser copy too.
      try {
        const bin = atob(ev.zip_b64 || "");
        const arr = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        const blob = new Blob([arr], { type: "application/zip" });
        if (!blob.size) break;
        if (backupUrl) { try { URL.revokeObjectURL(backupUrl); } catch (_) {} }
        backupBlob = blob; backupBuilding = ev.building || building; backupUrl = URL.createObjectURL(blob);
        const dl = $("downloadLink");
        dl.href = backupUrl; dl.setAttribute("download", backupBuilding + "_design.zip"); show(dl, true);
        const note = $("backupNote"); if (note) { note.textContent = "✓ saved to your browser — Download & View work even after a restart"; show(note, true); }
        zipExtract(blob, "report.html").then((html) => {
          if (!html) return;
          if (reportUrl) { try { URL.revokeObjectURL(reportUrl); } catch (_) {} }
          reportUrl = URL.createObjectURL(html);
          const a = $("reportLink"); a.href = reportUrl; a.setAttribute("target", "_blank"); show(a, true);
        }).catch(() => {});
        blobifyViewer(blob);
      } catch (_) {}
      break;
    }
  }
}

let runController = null;
function setRunning(on) {
  $("runBtn").disabled = on; $("resumeBtn").disabled = on;
  $("brand").classList.toggle("running", on);
  show($("stopBtn"), on); show($("stopInfo"), on);
  if (!on) setLights([]);
}

async function startRun(resume, _retry) {
  const building = ($("building").value || "Project").trim();
  const brief = $("brief").value.trim();
  if (!resume && !brief) { $("runStatus").textContent = "enter a brief first"; return; }
  setRunning(true);
  setLights(["thinking"]);
  $("runStatus").textContent = resume ? "resuming…" : "running…";
  show($("reportLink"), false); show($("downloadLink"), false);
  if (!resume) { logEl().innerHTML = ""; reasonEl().innerHTML = ""; reasonLine = null; $("tokensLast").textContent = ""; $("tokensCum").textContent = ""; show($("tokensSep"), false); show($("tokensInfoCtx"), false); show($("tokensInfo"), false); }
  tokenLine = null;
  runController = new AbortController();
  let snapB64 = null;   // on resume, carry the in-browser snapshot so the server can rehydrate a missing job in THIS request
  if (resume && backupBlob && backupBuilding === building) { try { snapB64 = await _blobToB64(backupBlob); } catch (_) {} }
  let r;
  try {
    r = await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ building, brief, resume: !!resume,
            images: attachedImages.map(({ name, type, data_url }) => ({ name, type, data_url })),
            snapshot_b64: snapB64 }),
        signal: runController.signal });
  } catch (e) { finishRun("stopped"); return; }
  if (!r.ok) {
    const detail = ((await r.json().catch(() => ({}))).detail) || ("HTTP " + r.status);
    // The server may have restarted and lost the in-memory creds -> re-send them from the Settings
    // fields (still held in the browser, never persisted server-side) and retry the run once, transparently.
    if (!_retry && /base-url|API key|Settings/i.test(detail) && $("apiKey").value.trim()) {
      await jpost("/api/creds", { base_url: $("baseUrl").value.trim(), api_key: $("apiKey").value,
          model: $("model").value.trim(), reasoning: $("reasoning").value,
          max_tokens: parseInt($("maxTokens").value, 10) || 32000, provider: $("provider").value.trim() });
      return startRun(resume, true);
    }
    logLine("err", "✖ " + detail);
    finishRun("failed"); return;
  }
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  try {
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const line = block.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        try { handleEvent(JSON.parse(line.slice(5).trim()), building); } catch (_) {}
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      logLine("err", "✖ connection lost mid-run: " + e);
      logLine("status", "· your progress IS saved — click Continue to resume from where it stopped");
      $("runStatus").textContent = "connection lost — click Continue to resume";
      backupWithRetry(building, [1500, 6000]);
      finishRun("connection lost"); return;
    }
  }
  finishRun("done");
}
function finishRun(status) {
  setRunning(false); show($("resumeBtn"), true);
  const cur = $("runStatus").textContent;
  if (cur === "running…" || cur === "resuming…") $("runStatus").textContent = status;
  runController = null;
}

// ---------------- in-browser backup + recovery ----------------
// The unified zip from /api/download is BOTH the user's deliverable and the resume snapshot. At each safe
// checkpoint we pull it into the tab's memory and point Download at that in-memory copy. On Continue, if
// the server no longer has the job (e.g. cleared data dir), we push this snapshot back before resuming.
async function backupResults(building) {
  if (!building) return false;
  try {
    const r = await api("/api/download/" + encodeURIComponent(building));
    if (!r.ok) return false;
    const blob = await r.blob();
    if (!blob || !blob.size) return false;
    if (backupUrl) { try { URL.revokeObjectURL(backupUrl); } catch (_) {} }
    backupBlob = blob; backupBuilding = building; backupUrl = URL.createObjectURL(blob);
    const dl = $("downloadLink");
    dl.href = backupUrl; dl.setAttribute("download", building + "_design.zip"); show(dl, true);
    const note = $("backupNote");
    if (note) { note.textContent = "✓ saved to your browser — Download anytime; also restores Continue"; show(note, true); }
    zipExtract(blob, "report.html").then((html) => {   // enable offline View from the backup too
      if (!html) return;
      if (reportUrl) { try { URL.revokeObjectURL(reportUrl); } catch (_) {} }
      reportUrl = URL.createObjectURL(html);
      const a = $("reportLink"); a.href = reportUrl; a.setAttribute("target", "_blank"); show(a, true);
    }).catch(() => {});
    blobifyViewer(blob);
    return true;
  } catch (_) { return false; }
}
// Stop/Pause/connection-loss path: the SSE stream is gone, so pull the bundle over a plain request instead;
// retry a few times (the server may still be finishing its last tool call) and say so if nothing exists yet.
async function backupWithRetry(building, delays = [1200, 4000, 10000]) {
  for (const d of delays) {
    await new Promise((r) => setTimeout(r, d));
    if (await backupResults(building)) return true;
  }
  const note = $("backupNote");
  if (note) { note.textContent = "nothing to download yet — the run stopped before any results were saved"; show(note, true); }
  return false;
}
function _blobToB64(blob) {   // -> base64 (no data: prefix), for shipping the snapshot inside the run request
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",", 2)[1] || ""); r.onerror = rej; r.readAsDataURL(blob); });
}
// Pull one file out of an in-memory zip Blob (DEFLATE) so the report can be VIEWED offline from the backup,
// no server round-trip. Parses the zip central directory and inflates via DecompressionStream.
async function zipExtract(blob, wantName) {
  try {
    const buf = new Uint8Array(await blob.arrayBuffer());
    const dv = new DataView(buf.buffer);
    let eocd = -1;
    for (let i = buf.length - 22; i >= 0; i--) { if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; } }
    if (eocd < 0) return null;
    let p = dv.getUint32(eocd + 16, true); const count = dv.getUint16(eocd + 10, true);
    for (let n = 0; n < count && dv.getUint32(p, true) === 0x02014b50; n++) {
      const method = dv.getUint16(p + 10, true), compSize = dv.getUint32(p + 20, true);
      const nameLen = dv.getUint16(p + 28, true), extraLen = dv.getUint16(p + 30, true), commentLen = dv.getUint16(p + 32, true);
      const lho = dv.getUint32(p + 42, true);
      const fname = new TextDecoder().decode(buf.subarray(p + 46, p + 46 + nameLen));
      if (fname === wantName || fname.endsWith("/" + wantName)) {
        const start = lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true);
        const comp = buf.subarray(start, start + compSize);
        if (method === 0) return new Blob([comp], { type: "text/html" });
        const inflated = await new Response(new Blob([comp]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer();
        return new Blob([inflated], { type: "text/html" });
      }
      p += 46 + nameLen + extraLen + commentLen;
    }
  } catch (_) {}
  return null;
}
$("runBtn").addEventListener("click", () => startRun(false));
$("resumeBtn").addEventListener("click", () => startRun(true));
async function loadExample(which, label) {
  try {
    const j = await (await api("/api/example/" + which)).json();
    if (j && j.brief) {
      $("brief").value = j.brief;
      if (!$("building").value.trim()) $("building").value = (which || "example").replace(/^ex/, "Ex");
      $("runStatus").textContent = "example loaded (" + (label || which) + ") — edit if you like, then click Design building";
    } else { $("runStatus").textContent = "could not load example"; }
  } catch (_) { $("runStatus").textContent = "could not load example"; }
}
$("exampleSelect").addEventListener("change", (e) => {
  const sel = e.target;
  if (!sel.value) return;
  loadExample(sel.value, sel.options[sel.selectedIndex].text);
  sel.selectedIndex = 0;                     // reset so the same example can be re-picked
});
async function stopServerRun() {
  const building = ($("building").value || "Project").trim();
  try { await jpost("/api/stop", { building }); } catch (_) {}
}
let stopWaitTimer = null;
$("stopBtn").addEventListener("click", () => {
  const b = ($("building").value || "Project").trim();
  stopServerRun();                                   // the run loop polls the cancel flag and halts within a few seconds
  $("runStatus").textContent = "stopping — saving your progress…";
  logLine("status", "· stop requested — the run will halt within a few seconds and send your files to this tab");
  if (stopWaitTimer) clearTimeout(stopWaitTimer);
  stopWaitTimer = setTimeout(() => {                 // fallback: disconnect teardown
    stopWaitTimer = null;
    if (runController) runController.abort();
    $("runStatus").textContent = "stopped — progress saved; click Continue to resume";
    backupWithRetry(b);
  }, 20000);
});

// ---------------- file upload (text/PDF -> brief, images -> attachments; max 3 files, 5 MB each) ----------------
const MAX_FILES = 3, MAX_BYTES = 5 * 1024 * 1024;
const IMG_RE = /\.(png|jpe?g|gif|webp)$/i, TXT_RE = /\.(txt|md|markdown|text)$/i, PDF_RE = /\.pdf$/i;
function fmtSize(n) { return n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
function renderFileList() {
  const el = $("fileList"); el.innerHTML = "";
  attachedImages.forEach((im, i) => {
    const c = document.createElement("span"); c.className = "filechip";
    const b = document.createElement("b"); b.textContent = im.name;
    const s = document.createElement("span"); s.className = "sz"; s.textContent = fmtSize(im.size);
    const x = document.createElement("span"); x.className = "x"; x.title = "remove"; x.textContent = "✕";
    x.addEventListener("click", () => { attachedImages.splice(i, 1); renderFileList(); });
    c.append(b, s, x); el.appendChild(c);
  });
}
const _readFile = (f, how) => new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r[how](f); });
// pdf.js is only fetched the first time a PDF is added (keeps normal page loads light)
let _pdfjsP = null;
function _loadPdfjs() {
  if (_pdfjsP) return _pdfjsP;
  _pdfjsP = new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js";
    s.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js"; res(window.pdfjsLib); };
    s.onerror = () => { _pdfjsP = null; rej(new Error("pdf.js failed to load")); };
    document.head.appendChild(s);
  });
  return _pdfjsP;
}
async function _pdfText(f) {                        // extract the text layer, page by page
  const lib = await _loadPdfjs();
  const doc = await lib.getDocument({ data: await _readFile(f, "readAsArrayBuffer") }).promise;
  const pages = [];
  for (let p = 1; p <= doc.numPages; p++) {
    const tc = await (await doc.getPage(p)).getTextContent();
    pages.push(tc.items.map((it) => it.str).join(" "));
  }
  try { doc.destroy(); } catch (_) {}
  return pages.join("\n\n").replace(/[ \t]+/g, " ").trim();
}
async function addFiles(files) {
  files = Array.from(files || []); if (!files.length) return;
  if (files.length > MAX_FILES) { $("briefFileName").textContent = `max ${MAX_FILES} files at once`; return; }
  const textChunks = [], txtNames = []; let note = "";
  for (const f of files) {
    if (f.size > MAX_BYTES) { note = `"${f.name}" exceeds 5 MB — skipped`; continue; }
    const isImg = IMG_RE.test(f.name) || /^image\//.test(f.type);
    const isPdf = PDF_RE.test(f.name) || f.type === "application/pdf";
    const isTxt = TXT_RE.test(f.name) || /^text\//.test(f.type) || f.type === "";
    if (isImg) {
      if (attachedImages.length >= MAX_FILES) { note = `max ${MAX_FILES} files`; continue; }
      attachedImages.push({ name: f.name, type: f.type || "image/*", data_url: await _readFile(f, "readAsDataURL"), size: f.size });
    } else if (isPdf) {
      try {
        const txt = await _pdfText(f);
        if (txt) { textChunks.push(`# ${f.name}\n` + txt); txtNames.push(f.name); }
        else note = `"${f.name}" has no extractable text (scanned PDF?) — skipped`;
      } catch (_) { note = `"${f.name}" could not be read as a PDF — skipped`; }
    } else if (isTxt) {
      textChunks.push((files.length > 1 ? `# ${f.name}\n` : "") + await _readFile(f, "readAsText")); txtNames.push(f.name);
    } else { note = `"${f.name}" type not supported — skipped`; }
  }
  if (textChunks.length) {
    const cur = $("brief").value.trim();
    $("brief").value = (cur ? cur + "\n\n" : "") + textChunks.join("\n\n");
  }
  $("briefFileName").textContent = txtNames.length ? "loaded " + txtNames.join(", ") + (note ? " · " + note : "") : note;
  renderFileList();
}
$("briefFile").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

// Manual recovery for a new machine / cleared data dir: load the design .zip into the browser; the next
// Continue ships it inside the run request, which restores + resumes in one step.
$("restoreFile").addEventListener("change", async (e) => {
  const f = (e.target.files || [])[0]; e.target.value = "";
  if (!f) return;
  const building = ($("building").value || "").trim();
  if (!building) { $("runStatus").textContent = "enter the project name first, then choose its resume .zip"; return; }
  if (backupUrl) { try { URL.revokeObjectURL(backupUrl); } catch (_) {} }
  backupBlob = f; backupBuilding = building; backupUrl = URL.createObjectURL(f);
  const dl = $("downloadLink"); dl.href = backupUrl; dl.setAttribute("download", building + "_design.zip"); show(dl, true);
  zipExtract(f, "report.html").then((html) => { if (!html) return; if (reportUrl) { try { URL.revokeObjectURL(reportUrl); } catch (_) {} } reportUrl = URL.createObjectURL(html); const a = $("reportLink"); a.href = reportUrl; a.setAttribute("target", "_blank"); show(a, true); }).catch(() => {});
  blobifyViewer(f);
  show($("resumeBtn"), true);
  $("runStatus").textContent = "loaded — type your change and click Continue to resume from this file";
});

$("building").addEventListener("change", loadJobLog);
async function loadJobLog() {
  const b = ($("building").value || "").trim(); if (!b) return;
  try {
    const j = await (await api("/api/log/" + encodeURIComponent(b))).json();
    if (j.events && j.events.length) { logEl().innerHTML = ""; tokenLine = null; j.events.forEach((ev) => handleEvent(ev, b)); $("runStatus").textContent = "loaded previous log"; }
    show($("resumeBtn"), !!j.resumable);
    if (j.has_report) { $("reportLink").href = `/api/report/${encodeURIComponent(b)}/report.html`; show($("reportLink"), true);
      $("modelLink").href = `/api/report/${encodeURIComponent(b)}/viewer_3d.html`; show($("modelLink"), true);
      $("downloadLink").href = `/api/download/${encodeURIComponent(b)}`; show($("downloadLink"), true); }
    if (j.resumable || j.has_report) backupResults(b);   // keep an in-browser copy too
  } catch (_) {}
}

function wireExpand(btnId, targetId) {
  const b = $(btnId), t = $(targetId);
  if (!b || !t) return;
  b.addEventListener("click", () => {
    const ex = t.classList.toggle("expanded");
    b.textContent = ex ? "⤡" : "⤢";
    b.title = ex ? "Shrink" : "Expand";
  });
}
wireExpand("logExpand", "log");
wireExpand("reasonExpand", "reason");

boot();


// ---- 3D viewer resilience --------------------------------------------------------------------
// In-browser copy of viewer_3d.html (mirrors reportUrl): once any bundle exists, "View model"
// serves from browser memory.
function blobifyViewer(zipBlob) {
  zipExtract(zipBlob, "viewer_3d.html").then((html) => {
    if (!html) return;
    if (modelUrl) { try { URL.revokeObjectURL(modelUrl); } catch (_) {} }
    modelUrl = URL.createObjectURL(html);
    const a = $("modelLink"); a.href = modelUrl; a.setAttribute("target", "_blank"); show(a, true);
  }).catch(() => {});
}
// No in-browser copy yet (early in a run / restored session): fetch with retries instead of making
// the user press the button repeatedly. The tab must be opened synchronously (popup rules), then
// pointed at the fetched copy.
$("modelLink").addEventListener("click", (e) => {
  const href = $("modelLink").getAttribute("href") || "";
  if (!href || href.startsWith("blob:")) return;              // in-browser copy: default behavior
  e.preventDefault();
  const w = window.open("", "_blank");
  (async () => {
    for (let i = 0; i < 4; i++) {
      try {
        const r = await fetch(href, { cache: "no-store" });
        if (r.ok) {
          const u = URL.createObjectURL(await r.blob());
          if (w) w.location = u; else window.open(u, "_blank", "noopener");
          return;
        }
      } catch (_) {}
      if (w) { try { w.document.body.textContent = "loading 3D viewer… retry " + (i + 1) + "/4"; } catch (_) {} }
      await new Promise((res) => setTimeout(res, 1200 * (i + 1)));
    }
    if (w) { try { w.close(); } catch (_) {} }
    $("runStatus").textContent = "3D viewer not reachable right now — open viewer_3d.html from the Download zip";
  })();
});
