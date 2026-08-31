"use strict";

const $ = (id) => document.getElementById(id);

const EXAMPLES = [
  "webcam at the top",
  "my webcam is bottom right",
  "square, bigger webcam",
  "gameplay only, no webcam",
  "follow my face",
  "zoom in tighter on my face",
  "3 clips",
  "cut 14:45 to 15:30",
];

let source = null;      // { source, name }
let specTimer = null;
let es = null;          // EventSource

// ---------- layout preview ----------

// Fits the preview box inside a sane area while keeping the true aspect ratio,
// so "square" and "vertical" are visibly different at a glance.
function drawPreview(spec, summary, notes, warning) {
  const maxH = 300, maxW = 260;
  const ratio = spec.width / spec.height;
  let h = maxH, w = h * ratio;
  if (w > maxW) { w = maxW; h = w / ratio; }

  const frame = $("frame");
  frame.style.width = Math.round(w) + "px";
  frame.style.height = Math.round(h) + "px";

  const stacked = spec.layout === "stacked";
  const cam = $("pvCam");
  cam.style.display = stacked ? "flex" : "none";
  if (stacked) cam.style.height = Math.round(h * spec.cam_panel_fraction) + "px";

  $("pvGame").textContent =
    spec.layout === "facetrack" ? "face-tracked crop"
    : spec.layout === "center" ? "centre crop"
    : "gameplay";

  $("dims").textContent = `${spec.width} × ${spec.height}`;
  $("summary").textContent = summary;
  $("notes").innerHTML = (notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  $("warn").innerHTML = warning ? `<div class="warn-box">${esc(warning)}</div>` : "";

  const ranges = spec.time_ranges || [];
  $("exact").innerHTML = ranges.length
    ? `<div class="exact-box"><b>Exact cuts — no AI ranking</b>${
        ranges.map((r) => `<div>${clock(r[0])} → ${clock(r[1])} <span>(${Math.round(r[1] - r[0])}s)</span></div>`).join("")
      }</div>`
    : "";
}

async function refreshPreview() {
  try {
    const r = await fetch("/api/layout/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: $("prompt").value, use_llm: true }),
    });
    if (!r.ok) return;
    const d = await r.json();
    drawPreview(d.spec, d.summary, d.notes, d.warning);
  } catch (_) { /* preview is cosmetic — never block on it */ }
}

// ---------- source handling ----------

function setSource(src, name) {
  source = { source: src, name: name || src };
  $("srcName").textContent = source.name;
  $("srcTag").classList.remove("hidden");
  $("go").disabled = false;
  $("go").textContent = "Generate shorts";
}

function clearSource() {
  source = null;
  $("srcTag").classList.add("hidden");
  $("chanWrap").classList.add("hidden");
  $("go").disabled = true;
  $("go").textContent = "Add a source first";
  $("url").value = "";
}

function showErr(msg) {
  $("srcErr").innerHTML = msg ? `<div class="err">${esc(msg)}</div>` : "";
}

async function loadUrl() {
  const url = $("url").value.trim();
  if (!url) return;
  showErr("");
  $("load").textContent = "…";
  $("load").disabled = true;
  try {
    const r = await fetch("/api/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not read that link");

    if (d.type === "video") {
      $("chanWrap").classList.add("hidden");
      setSource(d.source, url);
    } else {
      renderChannel(d.videos);
    }
  } catch (e) {
    showErr(e.message);
  } finally {
    $("load").textContent = "Load";
    $("load").disabled = false;
  }
}

function renderChannel(videos) {
  $("chanWrap").classList.remove("hidden");
  $("vids").innerHTML = videos.map((v, i) => `
    <div class="vid" data-i="${i}">
      ${v.thumbnail
        ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">`
        : `<div class="vthumb-none">🎬</div>`}
      <div class="vt">${esc(v.title)}</div>
      <div class="vd">${fmtDur(v.duration)}</div>
    </div>`).join("");

  [...$("vids").children].forEach((el) => {
    el.onclick = () => {
      [...$("vids").children].forEach((o) => o.classList.remove("sel"));
      el.classList.add("sel");
      const v = videos[+el.dataset.i];
      setSource(v.url, v.title);
    };
  });
}

async function uploadFile(file) {
  showErr("");
  $("drop").querySelector(".big").textContent = `Uploading ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Upload failed");
    setSource(d.source, `${d.name} (${fmtSize(d.size)})`);
    $("chanWrap").classList.add("hidden");
  } catch (e) {
    showErr(e.message);
  } finally {
    $("drop").querySelector(".big").textContent = "Drop a video file here";
  }
}

// ---------- run ----------

async function run() {
  if (!source) return;
  $("go").disabled = true;
  $("go").textContent = "Working…";
  $("results").classList.add("hidden");
  $("clips").innerHTML = "";
  $("log").textContent = "";
  $("progress").classList.add("on");
  showErr("");

  let job;
  try {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: source.source,
        prompt: $("prompt").value,
        download_format: $("format").value,
      }),
    });
    job = await r.json();
    if (!r.ok) throw new Error(job.detail || "Could not start the job");
  } catch (e) {
    showErr(e.message);
    $("go").disabled = false;
    $("go").textContent = "Generate shorts";
    return;
  }

  if (es) es.close();
  es = new EventSource(`/api/jobs/${job.id}/stream`);
  es.onmessage = (ev) => onUpdate(JSON.parse(ev.data));
  es.onerror = () => {
    // Stream drops when the job ends; fall back to one direct read.
    es.close();
    fetch(`/api/jobs/${job.id}`).then((r) => r.json()).then(onUpdate).catch(() => {});
  };
}

function onUpdate(s) {
  $("stageLabel").textContent = s.message || s.stage_label;
  $("pct").textContent = Math.round(s.progress * 100) + "%";
  $("barFill").style.transform = `scaleX(${Math.max(0, Math.min(1, s.progress))})`;
  if (s.log && s.log.length) {
    const el = $("log");
    const stuck = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = s.log.join("\n");
    if (stuck) el.scrollTop = el.scrollHeight;
  }
  if (s.status === "done" || s.status === "error") finish(s);
}

function finish(s) {
  if (es) { es.close(); es = null; }
  $("go").disabled = false;
  $("go").textContent = "Generate shorts";

  if (s.error && !(s.clips || []).some((c) => c.url)) {
    showErr(s.error);
    return;
  }

  const ok = (s.clips || []).filter((c) => c.url);
  if (!ok.length) return;

  $("results").classList.remove("hidden");
  $("clips").innerHTML = ok.map((c) => `
    <div class="clip">
      <video src="${esc(c.url)}" controls preload="metadata" playsinline></video>
      <div class="meta">
        <div class="t">${esc(c.title || "Untitled")}</div>
        <div class="r">
          <span class="badge">${c.score ?? "–"}</span>
          <span class="badge">${c.duration}s</span>
        </div>
        ${c.virality_reason ? `<div class="r" style="margin-top:7px">${esc(c.virality_reason)}</div>` : ""}
      </div>
      <a class="dl" href="${esc(c.url)}" download>Download</a>
    </div>`).join("");
}

// ---------- helpers ----------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function clock(sec) {
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtDur(sec) {
  if (!sec) return "";
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSize(b) {
  if (b > 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b > 1e6) return Math.round(b / 1e6) + " MB";
  return Math.round(b / 1e3) + " KB";
}

// ---------- wiring ----------

$("chips").innerHTML = EXAMPLES.map((e) => `<span class="chip">${esc(e)}</span>`).join("");
[...$("chips").children].forEach((c) => {
  c.onclick = () => {
    const t = $("prompt");
    t.value = t.value.trim() ? `${t.value.trim()}, ${c.textContent}` : c.textContent;
    // ---------- first-run setup ----------

async function checkSetup() {
  try {
    const d = await (await fetch("/api/settings")).json();
    if (!d.has_key) $("setup").classList.remove("hidden");
    if (!d.ffmpeg) {
      $("srcErr").innerHTML = `<div class="err">ffmpeg isn't on your PATH — clips can't be rendered without it. `
        + `Install it from ffmpeg.org, then restart this app.</div>`;
    }
  } catch (_) { /* offline settings check is not worth blocking startup */ }
}

$("setProvider").onchange = () => {
  const gem = $("setProvider").value === "gemini";
  $("keyLink").textContent = gem ? "Get a free Gemini key →" : "Get an OpenAI key →";
  $("keyLink").href = gem
    ? "https://aistudio.google.com/apikey"
    : "https://platform.openai.com/api-keys";
};

$("setSave").onclick = async () => {
  const key = $("setKey").value.trim();
  if (!key) { $("setMsg").innerHTML = `<div class="err">Paste a key first.</div>`; return; }
  $("setSave").disabled = true;
  $("setSave").textContent = "Saving…";
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: $("setProvider").value, api_key: key }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not save");
    $("setMsg").innerHTML = `<div class="ok-box">Saved. You're ready to go.</div>`;
    setTimeout(() => $("setup").classList.add("hidden"), 1200);
  } catch (e) {
    $("setMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    $("setSave").disabled = false;
    $("setSave").textContent = "Save and continue";
  }
};

checkSetup();
refreshPreview();
  };
});

$("prompt").addEventListener("input", () => {
  clearTimeout(specTimer);
  specTimer = setTimeout(refreshPreview, 450);
});

$("drop").onclick = () => $("file").click();
$("file").onchange = (e) => e.target.files[0] && uploadFile(e.target.files[0]);

["dragenter", "dragover"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => { e.preventDefault(); $("drop").classList.remove("over"); }));
$("drop").addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

$("load").onclick = loadUrl;
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") loadUrl(); });
$("srcClear").onclick = clearSource;
$("go").onclick = run;

// ---------- first-run setup ----------

async function checkSetup() {
  try {
    const d = await (await fetch("/api/settings")).json();
    if (!d.has_key) $("setup").classList.remove("hidden");
    if (!d.ffmpeg) {
      $("srcErr").innerHTML = `<div class="err">ffmpeg isn't on your PATH — clips can't be rendered without it. `
        + `Install it from ffmpeg.org, then restart this app.</div>`;
    }
  } catch (_) { /* offline settings check is not worth blocking startup */ }
}

$("setProvider").onchange = () => {
  const gem = $("setProvider").value === "gemini";
  $("keyLink").textContent = gem ? "Get a free Gemini key →" : "Get an OpenAI key →";
  $("keyLink").href = gem
    ? "https://aistudio.google.com/apikey"
    : "https://platform.openai.com/api-keys";
};

$("setSave").onclick = async () => {
  const key = $("setKey").value.trim();
  if (!key) { $("setMsg").innerHTML = `<div class="err">Paste a key first.</div>`; return; }
  $("setSave").disabled = true;
  $("setSave").textContent = "Saving…";
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: $("setProvider").value, api_key: key }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not save");
    $("setMsg").innerHTML = `<div class="ok-box">Saved. You're ready to go.</div>`;
    setTimeout(() => $("setup").classList.add("hidden"), 1200);
  } catch (e) {
    $("setMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    $("setSave").disabled = false;
    $("setSave").textContent = "Save and continue";
  }
};

checkSetup();
refreshPreview();
