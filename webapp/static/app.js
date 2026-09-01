"use strict";

/* Stream to Shorts — front end.
   Sections: helpers · chrome · setup · source · layout · run · results
             · player · trim · mini player */

const $ = (id) => document.getElementById(id);
const body = document.body;

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

const STEPS = [
  ["download", "Fetching"],
  ["transcribe", "Transcribing"],
  ["rank", "Ranking"],
  ["render", "Rendering"],
];

let source = null;       // { source, name }
let specTimer = null;
let es = null;           // EventSource
let jobId = null;
let clips = [];          // whatever the server last told us about this job
let cur = -1;            // index of the clip in the player
let trim = null;         // { lo, hi, start, end }
let confirmFn = null;

// ---------------------------------------------------------------- helpers

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function clock(sec) {
  const t = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

// Accepts "1:02:03", "14:45", or bare seconds — whatever the user types.
function parseClock(text) {
  const raw = String(text || "").trim();
  if (!raw) return NaN;
  if (!raw.includes(":")) return parseFloat(raw);
  return raw.split(":").reduce((acc, part) => acc * 60 + (parseFloat(part) || 0), 0);
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

let toastTimer = null;
function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("bad", !!bad);
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), bad ? 5200 : 3000);
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `Request failed (${r.status})`);
  return d;
}

function json(method, payload) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
}

function loadbar(frac) {
  const el = $("loadbar");
  if (frac === null) { el.classList.remove("on"); el.firstElementChild.style.transform = "scaleX(0)"; return; }
  el.classList.add("on");
  el.firstElementChild.style.transform = `scaleX(${Math.max(0, Math.min(1, frac))})`;
}

function ask(title, text, onYes) {
  $("cTitle").textContent = title;
  $("cText").textContent = text;
  confirmFn = onYes;
  $("confirm").classList.remove("hidden");
  body.classList.add("confirm-on");
}

function closeAsk() {
  $("confirm").classList.add("hidden");
  body.classList.remove("confirm-on");
  confirmFn = null;
}

// ---------------------------------------------------------------- chrome

$("menuBtn").onclick = () => {
  // Wide screens hide the rail; narrow ones reveal it over the content.
  if (window.innerWidth > 1000) body.classList.toggle("guide-off");
  else body.classList.toggle("guide-on");
};

document.querySelectorAll(".g-item[data-go]").forEach((item) => {
  item.onclick = () => {
    const el = $(item.dataset.go);
    if (!el || el.classList.contains("hidden")) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    if (window.innerWidth <= 1000) body.classList.remove("guide-on");
  };
});

// Highlight whichever section is on screen, the way a guide rail should.
const spy = new IntersectionObserver((entries) => {
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    document.querySelectorAll(".g-item[data-go]").forEach((i) =>
      i.classList.toggle("active", i.dataset.go === e.target.id));
  });
}, { rootMargin: "-15% 0px -70% 0px" });
["secCreate", "secLayout", "results", "secHelp"].forEach((id) => {
  const el = $(id);
  if (el) spy.observe(el);
});

function openDrawer() {
  body.classList.add("drawer-on");
  loadLocations();
  loadCleanup();
}
$("settingsBtn").onclick = openDrawer;
$("gSettings").onclick = openDrawer;
$("drawerClose").onclick = () => body.classList.remove("drawer-on");
$("scrim").onclick = () => {
  body.classList.remove("drawer-on");
  if (confirmFn) closeAsk();
};

$("createBtn").onclick = () => {
  $("secCreate").scrollIntoView({ behavior: "smooth", block: "start" });
  $("url").focus();
};

$("cNo").onclick = closeAsk;
$("cYes").onclick = () => { const f = confirmFn; closeAsk(); if (f) f(); };

// ---------------------------------------------------------------- settings

async function checkSetup() {
  try {
    const d = await api("/api/settings");
    if (!d.has_key) $("setup").classList.remove("hidden");
    if (d.provider) {
      $("setProvider").value = d.provider;
      $("setProvider2").value = d.provider;
    }
    $("setInfo").innerHTML = `
      <div><span>Key</span><b>${d.has_key ? "set — from " + esc(d.source) : "not set"}</b></div>
      <div><span>Model</span><b>${esc(d.model || "—")}</b></div>
      <div><span>ffmpeg</span><b>${d.ffmpeg ? "found" : "missing"}</b></div>`;
    if (!d.ffmpeg) {
      $("srcErr").innerHTML = `<div class="err">ffmpeg isn't on your PATH — clips can't be rendered without it. `
        + `Install it from ffmpeg.org, then restart this app.</div>`;
    }
  } catch (_) { /* an offline settings check is not worth blocking startup */ }
}

function keyLinkFor(provider) {
  const gem = provider === "gemini";
  $("keyLink").textContent = gem ? "Get a free Gemini key →" : "Get an OpenAI key →";
  $("keyLink").href = gem ? "https://aistudio.google.com/apikey"
                          : "https://platform.openai.com/api-keys";
}
$("setProvider").onchange = () => keyLinkFor($("setProvider").value);

async function saveKey(providerEl, keyEl, btn, msgEl, onDone) {
  const key = keyEl.value.trim();
  if (!key) { msgEl.innerHTML = `<div class="err">Paste a key first.</div>`; return; }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await api("/api/settings", json("POST", { provider: providerEl.value, api_key: key }));
    msgEl.innerHTML = `<div class="ok-box">Saved. You're ready to go.</div>`;
    keyEl.value = "";
    checkSetup();
    if (onDone) setTimeout(onDone, 1100);
  } catch (e) {
    msgEl.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

$("setSave").onclick = () =>
  saveKey($("setProvider"), $("setKey"), $("setSave"), $("setMsg"),
          () => $("setup").classList.add("hidden"));

$("setSave2").onclick = () =>
  saveKey($("setProvider2"), $("setKey2"), $("setSave2"), $("setMsg2"), null);

// ---------------------------------------------------------------- locations

let locations = null;

async function loadLocations() {
  try {
    locations = await api("/api/locations");
    if (!$("locPath").value) $("locPath").value = locations.root;
  } catch (_) { /* the drawer still works without it */ }
}

$("locSave").onclick = async () => {
  const path = $("locPath").value.trim();
  if (!path) { $("locMsg").innerHTML = `<div class="err">Enter a folder first.</div>`; return; }
  $("locSave").disabled = true;
  try {
    locations = await api("/api/locations", json("POST", { path }));
    $("locPath").value = locations.root;
    $("locMsg").innerHTML = `<div class="ok-box">Saved. New clips land in ${esc(locations.shorts)}</div>`;
  } catch (e) {
    $("locMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    $("locSave").disabled = false;
  }
};

let cleanup = null;

function humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

async function loadCleanup() {
  const info = $("cleanInfo"), btn = $("cleanBtn");
  try {
    cleanup = await api("/api/cleanup");
  } catch (_) {
    info.innerHTML = `<div><span>Couldn't check the folder</span></div>`;
    btn.disabled = true;
    return;
  }
  if (!cleanup.count) {
    info.innerHTML = `<div><span>Nothing to clear</span><b>0 B</b></div>`;
    btn.disabled = true;
    return;
  }
  const partials = cleanup.items.filter(i => i.kind === "partial").length;
  const sources = cleanup.count - partials;
  const rows = [`<div><span>Source videos</span><b>${sources}</b></div>`];
  if (partials) rows.push(`<div><span>Unfinished downloads</span><b>${partials}</b></div>`);
  rows.push(`<div><span>Frees up</span><b>${humanBytes(cleanup.bytes)}</b></div>`);
  info.innerHTML = rows.join("");
  btn.disabled = false;
}

$("cleanBtn").onclick = () => {
  if (!cleanup || !cleanup.count) return;
  ask(
    "Clear space?",
    `${cleanup.count} file${cleanup.count === 1 ? "" : "s"} will be deleted, freeing ${humanBytes(cleanup.bytes)}. `
      + `Your clips stay where they are. Making more shorts from the same video will download it again.`,
    async () => {
      $("cleanBtn").disabled = true;
      $("cleanMsg").innerHTML = "";
      try {
        const r = await api("/api/cleanup", json("POST", {}));
        toast(`Freed ${humanBytes(r.freed)}`);
        if (r.failed.length) {
          $("cleanMsg").innerHTML = `<div class="err">${esc(r.failed.join("; "))}</div>`;
        }
      } catch (e) {
        $("cleanMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
      } finally {
        await loadCleanup();
      }
    }
  );
};

async function openFolder() {
  try {
    if (!locations) await loadLocations();
    await api("/api/reveal", json("POST", { path: locations.shorts }));
  } catch (e) {
    toast(e.message, true);
  }
}
$("folderBtn").onclick = openFolder;
$("folderBtn2").onclick = openFolder;
$("gFolder").onclick = openFolder;

async function openYouTubeUpload() {
  try {
    await api("/api/open-upload", json("POST", {}));
    toast("YouTube's upload page is open in your browser.");
  } catch (e) {
    toast(e.message, true);
  }
}
$("uploadBtn").onclick = openYouTubeUpload;
$("gUpload").onclick = openYouTubeUpload;

// ---------------------------------------------------------------- source

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
  $("urlClear").classList.add("hidden");
}

function showErr(msg) {
  $("srcErr").innerHTML = msg ? `<div class="err">${esc(msg)}</div>` : "";
}

async function loadUrl() {
  const url = $("url").value.trim();
  if (!url) return;
  showErr("");
  $("load").disabled = true;
  loadbar(0.4);
  try {
    const d = await api("/api/resolve", json("POST", { url }));
    if (d.type === "video") {
      $("chanWrap").classList.add("hidden");
      setSource(d.source, url);
    } else {
      renderChannel(d.videos);
    }
    $("secCreate").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    showErr(e.message);
  } finally {
    $("load").disabled = false;
    loadbar(1);
    setTimeout(() => loadbar(null), 400);
  }
}

function renderChannel(videos) {
  $("chanWrap").classList.remove("hidden");
  $("vids").innerHTML = videos.map((v, i) => `
    <div class="vid" data-i="${i}">
      ${v.thumbnail
        ? `<img src="${esc(v.thumbnail)}" alt="" loading="lazy">`
        : `<div class="vthumb-none"><svg><use href="#i-film"/></svg></div>`}
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
  const drop = $("drop");
  const big = drop.querySelector(".big");
  drop.classList.add("busy");
  big.textContent = `Copying ${file.name}…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await api("/api/upload", { method: "POST", body: fd });
    setSource(d.source, `${d.name} · ${fmtSize(d.size)}`);
    $("chanWrap").classList.add("hidden");
  } catch (e) {
    showErr(e.message);
  } finally {
    drop.classList.remove("busy");
    big.textContent = "Drag a video here";
  }
}

$("drop").onclick = () => $("file").click();
$("pickFile").onclick = () => $("file").click();
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
$("url").addEventListener("input", () =>
  $("urlClear").classList.toggle("hidden", !$("url").value));
$("urlClear").onclick = () => {
  $("url").value = "";
  $("urlClear").classList.add("hidden");
  $("url").focus();
};
$("srcClear").onclick = clearSource;

// ---------------------------------------------------------------- layout

function drawPreview(spec, summary, notes, warning) {
  const maxH = 300, maxW = 250;
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
    const d = await api("/api/layout/preview", json("POST", { prompt: $("prompt").value, use_llm: true }));
    drawPreview(d.spec, d.summary, d.notes, d.warning);
  } catch (_) { /* the preview is cosmetic — never block on it */ }
}

$("chips").innerHTML = EXAMPLES.map((e) =>
  `<button class="chip" data-t="${esc(e)}">${esc(e)}</button>`).join("");

function syncChips() {
  const text = $("prompt").value.toLowerCase();
  [...$("chips").children].forEach((c) =>
    c.classList.toggle("on", text.includes(c.dataset.t.toLowerCase())));
}

[...$("chips").children].forEach((c) => {
  c.onclick = () => {
    const t = $("prompt");
    const phrase = c.dataset.t;
    const has = t.value.toLowerCase().includes(phrase.toLowerCase());
    if (has) {
      // Toggling off should not leave a stray comma behind.
      t.value = t.value.replace(new RegExp(`\\s*,?\\s*${phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`, "i"), "")
                       .replace(/^\s*,\s*/, "").trim();
    } else {
      t.value = t.value.trim() ? `${t.value.trim()}, ${phrase}` : phrase;
    }
    syncChips();
    refreshPreview();
  };
});

$("prompt").addEventListener("input", () => {
  syncChips();
  clearTimeout(specTimer);
  specTimer = setTimeout(refreshPreview, 450);
});

// ---------------------------------------------------------------- run

async function run() {
  if (!source) return;
  $("go").disabled = true;
  $("go").textContent = "Working…";
  $("results").classList.add("hidden");
  $("clips").innerHTML = "";
  $("log").textContent = "";
  $("progress").classList.remove("hidden");
  $("progress").scrollIntoView({ behavior: "smooth", block: "center" });
  showErr("");
  closeMini();

  let job;
  try {
    job = await api("/api/jobs", json("POST", {
      source: source.source,
      prompt: $("prompt").value,
      download_format: $("format").value,
    }));
  } catch (e) {
    showErr(e.message);
    $("go").disabled = false;
    $("go").textContent = "Generate shorts";
    return;
  }

  jobId = job.id;
  if (es) es.close();
  es = new EventSource(`/api/jobs/${job.id}/stream`);
  es.onmessage = (ev) => onUpdate(JSON.parse(ev.data));
  es.onerror = () => {
    // The stream drops when the job ends; fall back to one direct read.
    es.close();
    fetch(`/api/jobs/${job.id}`).then((r) => r.json()).then(onUpdate).catch(() => {});
  };
}
$("go").onclick = run;

function onUpdate(s) {
  $("stageLabel").textContent = s.message || s.stage_label;
  $("pct").textContent = Math.round(s.progress * 100) + "%";
  $("barFill").style.transform = `scaleX(${Math.max(0, Math.min(1, s.progress))})`;
  loadbar(s.progress);

  const at = STEPS.findIndex(([k]) => k === s.stage);
  $("steps").innerHTML = STEPS.map(([k, label], i) => {
    const cls = s.status === "done" || (at > -1 && i < at) ? "done" : (i === at ? "now" : "");
    return `<span class="st ${cls}">${label}</span>`;
  }).join("");

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
  setTimeout(() => loadbar(null), 600);

  if (s.error && !(s.clips || []).some((c) => c.url)) {
    showErr(s.error);
    return;
  }

  clips = (s.clips || []).filter((c) => c.url);
  renderClips();
  if (!clips.length) return;

  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
  // The whole point of the wait: show the first clip playing, straight away.
  setTimeout(() => openMini(0), 700);
  toast(`${clips.length} clip${clips.length > 1 ? "s" : ""} ready — click one to trim or save it.`);
}

// ---------------------------------------------------------------- results

function renderClips() {
  const count = clips.length;
  $("gCount").textContent = count;
  $("gCount").classList.toggle("hidden", !count);
  $("results").classList.toggle("hidden", !count);

  $("clips").innerHTML = clips.map((c, i) => `
    <div class="clip" data-i="${i}" style="animation-delay:${i * 55}ms">
      <div class="thumb">
        <video src="${esc(c.url)}#t=0.5" preload="metadata" muted playsinline></video>
        <div class="veil"><span class="pbtn"><svg><use href="#i-play"/></svg></span></div>
        ${c.score != null ? `<span class="rank">★ ${esc(c.score)}</span>` : ""}
        <span class="dur">${clock(c.duration)}</span>
        <div class="flag">
          ${c.edited ? `<span>trimmed</span>` : ""}
          ${c.muted ? `<span>muted</span>` : ""}
          ${c.saved_to ? `<span>saved</span>` : ""}
        </div>
      </div>
      <div class="ct">${esc(c.title || "Untitled")}</div>
      <div class="cm">${clock(c.start_time)} → ${clock(c.end_time)}</div>
    </div>`).join("");

  [...$("clips").children].forEach((el) => {
    el.onclick = () => openPlayer(+el.dataset.i);
  });
}

// ---------------------------------------------------------------- player

const vid = $("pVideo");

function openPlayer(i) {
  const c = clips[i];
  if (!c) return;
  cur = i;
  closeMini();

  vid.src = c.url;
  vid.muted = false;
  vid.currentTime = 0;
  $("pTitle").textContent = c.title || "Untitled";
  $("pMeta").textContent =
    `${clock(c.start_time)} → ${clock(c.end_time)} · ${c.duration}s`
    + (c.score != null ? ` · score ${c.score}` : "")
    + (c.muted ? " · muted" : "");
  $("pDownload").href = c.url;
  $("pDownload").setAttribute("download", c.file || "short.mp4");

  body.classList.add("player-on");
  $("player").classList.remove("trim-on");
  setMuteIcon();
  vid.play().catch(() => {});
}

function closePlayer() {
  body.classList.remove("player-on");
  $("player").classList.remove("trim-on");
  vid.pause();
}
$("pClose").onclick = closePlayer;
$("player").addEventListener("click", (e) => { if (e.target === $("player")) closePlayer(); });

function setPlayIcon() {
  const on = !vid.paused;
  $("pPlay").querySelector("use").setAttribute("href", on ? "#i-pause" : "#i-play");
  $("pPlay").querySelector("span").textContent = on ? "Pause" : "Play";
  $("player").classList.toggle("paused", vid.paused);
}

function togglePlay() {
  if (vid.paused) vid.play().catch(() => {}); else vid.pause();
}
$("pPlay").onclick = togglePlay;
$("pTap").onclick = togglePlay;
vid.addEventListener("play", setPlayIcon);
vid.addEventListener("pause", setPlayIcon);

function setMuteIcon() {
  $("pMute").querySelector("use").setAttribute("href", vid.muted ? "#i-mute" : "#i-vol");
  $("pMute").querySelector("span").textContent = vid.muted ? "Muted" : "Sound";
  $("pMute").classList.toggle("on", vid.muted);
}
$("pMute").onclick = () => { vid.muted = !vid.muted; setMuteIcon(); };

vid.addEventListener("timeupdate", () => {
  const f = vid.duration ? vid.currentTime / vid.duration : 0;
  $("pScrubFill").style.width = `${f * 100}%`;
  $("pScrubKnob").style.left = `${f * 100}%`;
  $("pNow").textContent = clock(vid.currentTime);
});
vid.addEventListener("loadedmetadata", () => { $("pDur").textContent = clock(vid.duration); });
vid.addEventListener("ended", () => { vid.currentTime = 0; vid.play().catch(() => {}); });

function scrubTo(e) {
  const r = $("pScrub").getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  if (vid.duration) vid.currentTime = f * vid.duration;
}
$("pScrub").addEventListener("pointerdown", (e) => {
  scrubTo(e);
  const move = (ev) => scrubTo(ev);
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

document.addEventListener("keydown", (e) => {
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (!body.classList.contains("player-on")) {
    if (e.key === "Escape" && body.classList.contains("drawer-on")) body.classList.remove("drawer-on");
    return;
  }
  const k = e.key.toLowerCase();
  if (e.key === "Escape") { closePlayer(); }
  else if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (k === "m") { vid.muted = !vid.muted; setMuteIcon(); }
  else if (k === "t") { toggleTrim(); }
  else if (e.key === "ArrowRight") { vid.currentTime = Math.min(vid.duration || 0, vid.currentTime + 2); }
  else if (e.key === "ArrowLeft") { vid.currentTime = Math.max(0, vid.currentTime - 2); }
  else if (e.key === "ArrowDown" && cur < clips.length - 1) { openPlayer(cur + 1); }
  else if (e.key === "ArrowUp" && cur > 0) { openPlayer(cur - 1); }
});

// ---------------------------------------------------------------- trim

function toggleTrim() {
  const p = $("player");
  if (p.classList.contains("trim-on")) { p.classList.remove("trim-on"); return; }
  if (!clips[cur]) return;
  resetTrim();
  p.classList.add("trim-on");
}
$("pTrimBtn").onclick = toggleTrim;
$("trimClose").onclick = () => $("player").classList.remove("trim-on");

function resetTrim() {
  const c = clips[cur];
  if (!c) return;
  const start = Number(c.start_time) || 0;
  const end = Number(c.end_time) || start + 30;
  const pad = Math.max(20, (end - start) * 0.8);
  trim = { lo: Math.max(0, start - pad), hi: end + pad, start, end };
  $("tMute").checked = !!c.muted;
  $("tMsg").innerHTML = "";
  drawTrim();
}
$("tReset").onclick = resetTrim;

function drawTrim() {
  if (!trim) return;
  const span = trim.hi - trim.lo || 1;
  const a = ((trim.start - trim.lo) / span) * 100;
  const b = ((trim.end - trim.lo) / span) * 100;
  $("tFill").style.left = `${a}%`;
  $("tFill").style.width = `${Math.max(0, b - a)}%`;
  $("tStartH").style.left = `${a}%`;
  $("tEndH").style.left = `${b}%`;
  $("tStart").value = clock(trim.start);
  $("tEnd").value = clock(trim.end);
  $("tLen").textContent = `${(trim.end - trim.start).toFixed(1)}s`;
  $("tScaleA").textContent = clock(trim.lo);
  $("tScaleB").textContent = clock(trim.hi);
}

function dragHandle(which, e) {
  const track = $("tTrack");
  const h = which === "start" ? $("tStartH") : $("tEndH");
  h.classList.add("drag");

  const move = (ev) => {
    const r = track.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    const t = trim.lo + f * (trim.hi - trim.lo);
    if (which === "start") trim.start = Math.min(t, trim.end - 1);
    else trim.end = Math.max(t, trim.start + 1);
    drawTrim();
  };
  const up = () => {
    h.classList.remove("drag");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  move(e);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
$("tStartH").addEventListener("pointerdown", (e) => { e.preventDefault(); dragHandle("start", e); });
$("tEndH").addEventListener("pointerdown", (e) => { e.preventDefault(); dragHandle("end", e); });

function commitField(which) {
  const v = parseClock(which === "start" ? $("tStart").value : $("tEnd").value);
  if (!isFinite(v)) { drawTrim(); return; }
  if (which === "start") trim.start = Math.max(0, Math.min(v, trim.end - 1));
  else trim.end = Math.max(trim.start + 1, v);
  trim.lo = Math.min(trim.lo, Math.max(0, trim.start - 5));
  trim.hi = Math.max(trim.hi, trim.end + 5);
  drawTrim();
}
["tStart", "tEnd"].forEach((id) => {
  const which = id === "tStart" ? "start" : "end";
  $(id).addEventListener("change", () => commitField(which));
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") commitField(which); });
});

document.querySelectorAll("[data-nudge]").forEach((b) => {
  b.onclick = () => {
    const [which, step] = b.dataset.nudge.split(":");
    const d = parseFloat(step);
    if (which === "start") trim.start = Math.max(0, Math.min(trim.start + d, trim.end - 1));
    else trim.end = Math.max(trim.start + 1, trim.end + d);
    trim.lo = Math.min(trim.lo, Math.max(0, trim.start - 5));
    trim.hi = Math.max(trim.hi, trim.end + 5);
    drawTrim();
  };
});

function busy(on, text) {
  $("pBusy").classList.toggle("hidden", !on);
  if (text) $("pBusyText").textContent = text;
}

$("tApply").onclick = async () => {
  const c = clips[cur];
  if (!c || !trim) return;
  $("tApply").disabled = true;
  busy(true, "Re-cutting from the source…");
  $("tMsg").innerHTML = "";
  vid.pause();
  try {
    const updated = await api(`/api/jobs/${jobId}/clips/${encodeURIComponent(c.file)}/trim`,
      json("POST", { start: trim.start, end: trim.end, mute: $("tMute").checked }));
    clips[cur] = updated;
    renderClips();
    // Cache-bust: the new render can reuse a name the browser already holds.
    vid.src = `${updated.url}?v=${Date.now()}`;
    $("pMeta").textContent =
      `${clock(updated.start_time)} → ${clock(updated.end_time)} · ${updated.duration}s`
      + (updated.muted ? " · muted" : "");
    $("pDownload").href = updated.url;
    $("pDownload").setAttribute("download", updated.file);
    vid.play().catch(() => {});
    toast("Clip re-cut.");
    $("player").classList.remove("trim-on");
  } catch (e) {
    $("tMsg").innerHTML = `<div class="err">${esc(e.message)}</div>`;
  } finally {
    busy(false);
    $("tApply").disabled = false;
  }
};

// ---------------------------------------------------------------- save / delete

$("pSave").onclick = async () => {
  const c = clips[cur];
  if (!c) return;
  $("pSave").disabled = true;
  try {
    const d = await api(`/api/jobs/${jobId}/clips/${encodeURIComponent(c.file)}/save`,
      json("POST", { name: (c.title || "short").slice(0, 60) }));
    clips[cur] = { ...c, saved_to: d.path };
    renderClips();
    toast(`Saved to ${d.path}`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("pSave").disabled = false;
  }
};

$("pDelete").onclick = () => {
  const c = clips[cur];
  if (!c) return;
  ask("Delete this clip?", `"${c.title || "Untitled"}" is removed from your PC. This cannot be undone.`,
    async () => {
      try {
        await api(`/api/jobs/${jobId}/clips/${encodeURIComponent(c.file)}`, { method: "DELETE" });
        clips.splice(cur, 1);
        renderClips();
        toast("Clip deleted.");
        if (!clips.length) { closePlayer(); closeMini(); return; }
        openPlayer(Math.min(cur, clips.length - 1));
      } catch (e) {
        toast(e.message, true);
      }
    });
};

// ---------------------------------------------------------------- mini player

const mvid = $("mVideo");

function openMini(i) {
  const c = clips[i];
  if (!c) return;
  cur = i;
  mvid.src = c.url;
  mvid.muted = true;          // autoplay only survives if it starts silent
  $("mTitle").textContent = c.title || "Untitled";
  $("mini").classList.remove("hidden");
  setMiniIcons();
  mvid.play().catch(() => {});
}

function closeMini() {
  $("mini").classList.add("hidden");
  mvid.pause();
  mvid.removeAttribute("src");
  mvid.load();
}

function setMiniIcons() {
  $("mPlay").querySelector("use").setAttribute("href", mvid.paused ? "#i-play" : "#i-pause");
  $("mMute").querySelector("use").setAttribute("href", mvid.muted ? "#i-mute" : "#i-vol");
}

$("mPlay").onclick = () => { if (mvid.paused) mvid.play().catch(() => {}); else mvid.pause(); };
$("mMute").onclick = () => { mvid.muted = !mvid.muted; setMiniIcons(); };
$("mClose").onclick = () => closeMini();
$("mExpand").onclick = () => openPlayer(cur);
mvid.addEventListener("play", setMiniIcons);
mvid.addEventListener("pause", setMiniIcons);
mvid.addEventListener("ended", () => { mvid.currentTime = 0; mvid.play().catch(() => {}); });
mvid.addEventListener("timeupdate", () => {
  $("mFill").style.width = `${(mvid.duration ? mvid.currentTime / mvid.duration : 0) * 100}%`;
});
$("mini").querySelector("video").onclick = () => openPlayer(cur);

$("pMini").onclick = () => {
  const t = vid.currentTime;
  closePlayer();
  openMini(cur);
  mvid.addEventListener("loadedmetadata", () => { mvid.currentTime = t; }, { once: true });
};

// ---------------------------------------------------------------- boot

keyLinkFor($("setProvider").value);
checkSetup();
loadLocations();
refreshPreview();
