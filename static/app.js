(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const form = $("#job-form");
  const urlInput = $("#url");
  const urlHint = $("#url-hint");
  const submit = $("#submit");
  const enhanceOptions = $("#enhance-options");
  const engineSelect = form.elements.engine;
  const engineHint = $("#engine-hint");
  const cookiesInput = $("#cookies");
  const rememberCookies = $("#remember-cookies");
  const authStatus = $("#auth-status");
  const advanced = $("#advanced");

  const statusCard = $("#status");
  const stageEl = $("#stage");
  const substageEl = $("#substage");
  const elapsedEl = $("#elapsed");
  const cancelBtn = $("#cancel");
  const barFill = $("#bar-fill");
  const chipsEl = $("#chips");
  const errorEl = $("#error");
  const warningsEl = $("#warnings");
  const resultsEl = $("#results");
  const capsEl = $("#caps");
  const template = $("#result-template");

  const STORAGE = { options: "reeldl.options", cookies: "reeldl.cookies" };
  let capabilities = null;
  let currentJob = null;
  let pollTimer = null;
  let tickTimer = null;

  // ------------------------------------------------------------------ helpers

  const fmtBytes = (n) => {
    if (n == null) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  };
  const fmtDuration = (s) => {
    if (s == null) return "";
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    return m ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
  };
  const fmtFps = (fps) => (Math.abs(fps - Math.round(fps)) < 0.05 ? String(Math.round(fps)) : fps.toFixed(2));
  const fmtElapsed = (seconds) => {
    const s = Math.max(0, Math.round(seconds));
    const m = Math.floor(s / 60);
    return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  };
  let toastTimer = null;
  const toast = (html) => {
    let el = document.querySelector(".toast");
    if (!el) { el = document.createElement("div"); el.className = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.innerHTML = html;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 6000);
  };

  const chip = (label, value) => {
    const span = document.createElement("span");
    span.className = "chip";
    span.innerHTML = `${label} <b></b>`;
    span.querySelector("b").textContent = value;
    return span;
  };

  const IG_URL_RE = /(?:https?:\/\/)?(?:[\w-]+\.)*(?:instagram\.com|instagr\.am|ig\.me|youtube\.com|youtu\.be|youtube-nocookie\.com|tiktok\.com|deviantart\.com|fav\.me)\/\S+/i;
  const extractUrl = (text) => {
    const match = (text || "").match(IG_URL_RE);
    return match ? match[0].replace(/[.,;:!?)"']+$/, "") : null;
  };

  const platformOf = (value) => {
    if (/youtube\.com|youtu\.be/i.test(value)) return "youtube";
    if (/tiktok\.com/i.test(value)) return "tiktok";
    if (/deviantart\.com|fav\.me/i.test(value)) return "deviantart";
    if (/instagram\.com|instagr\.am|ig\.me/i.test(value)) return "instagram";
    return null;
  };

  const classifyUrl = (value) => {
    if (!value) return null;
    const platform = platformOf(value);
    if (platform && platform !== "instagram") return platform;
    if (/\/stories\/highlights\//.test(value)) return "highlight";
    if (/\/stories\//.test(value)) return "story";
    if (/\/reels?\//.test(value)) return "reel";
    if (/\/p\//.test(value)) return "post";
    if (/\/tv\//.test(value)) return "igtv";
    if (/\/share\//.test(value)) return "share";
    return "unknown";
  };

  // ------------------------------------------------------------------ persistence

  const saveOptions = () => {
    const data = {
      mode: form.elements.mode.value,
      resolution: form.elements.resolution.value,
      fps: form.elements.fps.value,
      engine: form.elements.engine.value,
    };
    localStorage.setItem(STORAGE.options, JSON.stringify(data));
  };
  const loadOptions = () => {
    try {
      const data = JSON.parse(localStorage.getItem(STORAGE.options) || "{}");
      if (data.mode) form.elements.mode.value = data.mode;
      if (data.resolution) form.elements.resolution.value = data.resolution;
      if (data.fps) form.elements.fps.value = data.fps;
      if (data.engine) form.elements.engine.value = data.engine;
    } catch { /* ignore */ }
    const cookies = localStorage.getItem(STORAGE.cookies);
    if (cookies) {
      cookiesInput.value = cookies;
      rememberCookies.checked = true;
    }
  };
  const persistCookies = () => {
    if (rememberCookies.checked && cookiesInput.value.trim()) {
      localStorage.setItem(STORAGE.cookies, cookiesInput.value.trim());
    } else {
      localStorage.removeItem(STORAGE.cookies);
    }
  };

  // ------------------------------------------------------------------ UI state

  const syncModeUI = () => {
    const enhance = form.elements.mode.value === "enhance";
    enhanceOptions.classList.toggle("hidden", !enhance);
  };

  const PASTE_TIP = "Tip: paste anywhere on this page (⌘V / Ctrl+V) and the download starts immediately.";
  const browserLoginActive = () => !!(capabilities && capabilities.browser_login && capabilities.browser_login.active_for_you);

  const syncUrlHint = () => {
    const kind = classifyUrl(urlInput.value.trim());
    urlHint.className = "hint";
    if (!kind) { urlHint.textContent = PASTE_TIP; return; }
    if (kind === "story" || kind === "highlight") {
      const configured = capabilities && capabilities.stories_auth_configured;
      const pasted = cookiesInput.value.trim().length > 0;
      if (configured || pasted) {
        urlHint.textContent = "Story link — login is configured, you're good to go.";
        urlHint.classList.add("ok");
      } else if (browserLoginActive()) {
        urlHint.textContent = "Story link — your browser's Instagram login will be used automatically.";
        urlHint.classList.add("ok");
      } else {
        urlHint.textContent = "Stories need an Instagram login. Open Advanced and paste your sessionid cookie.";
        urlHint.classList.add("warn");
        advanced.open = true;
      }
    } else if (kind === "unknown") {
      urlHint.textContent = "Paste a link to an Instagram reel/post/story, a YouTube video, a TikTok or a DeviantArt deviation.";
    } else if (kind === "youtube") {
      urlHint.textContent = "YouTube — served natively up to the creator's upload resolution; enhancement only kicks in below the target.";
    } else if (kind === "tiktok") {
      urlHint.textContent = "TikTok — best available rendition, no watermark when the platform offers it.";
    } else if (kind === "deviantart") {
      urlHint.textContent = "DeviantArt — original image file, or the highest film rendition. Images are upscaled in Enhance mode.";
    } else {
      urlHint.textContent = kind === "share" ? "Share link — it will be resolved automatically." : "";
    }
  };

  const applyCapabilities = (caps) => {
    capabilities = caps;
    if (caps.enhancement && caps.enhancement.enabled === false) {
      const enhanceRadio = form.querySelector('input[name="mode"][value="enhance"]');
      enhanceRadio.disabled = true;
      enhanceRadio.parentElement.title = caps.enhancement.reason || "Enhancement is disabled on this server.";
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = "This server only downloads — it doesn't have the CPU for 4K 60 fps rendering. Run the app locally or on a real VM for Enhance mode.";
      enhanceOptions.parentNode.insertBefore(note, enhanceOptions);
      enhanceRadio.parentElement.querySelector("span").textContent = "Enhance to 4K 60 fps — off on this server";
      form.elements.mode.value = "original";
      syncModeUI();
      enhanceOptions.classList.add("hidden");
    }
    const ai = caps.ai || {};
    const aiOption = $("#engine-ai");
    if (!ai.available) {
      aiOption.disabled = true;
      aiOption.textContent = "AI · not installed on this server";
      if (engineSelect.value === "ai") engineSelect.value = "auto";
      engineHint.textContent = "ffmpeg on this server — AI binaries not installed.";
    } else {
      engineHint.textContent = `AI available: ${ai.upscale_model} + ${ai.interpolation_model}. Slow, experimental.`;
    }
    const summary = $("#advanced-summary");
    if (caps.stories_auth_configured) {
      authStatus.className = "hint ok";
      authStatus.textContent = "This server already has an Instagram session configured — stories work without pasting anything. You can still paste your own cookie to use your account instead.";
      summary.textContent = "Advanced · logins (Instagram configured on the server)";
    } else if (browserLoginActive()) {
      authStatus.className = "hint ok";
      const browsers = (caps.browser_login.browsers || []).slice(0, 3).map((b) => b[0].toUpperCase() + b.slice(1)).join(", ");
      authStatus.textContent = `You're on the computer running this server, so whenever a site demands a login the app uses the session from your own browser (${browsers}…) automatically — nothing to paste. Only fill the box below if that fails (e.g. Safari needs Full Disk Access to be readable).`;
      summary.textContent = "Advanced · logins (automatic from your browser)";
    } else {
      authStatus.className = "hint";
      authStatus.textContent = "This server has no Instagram session configured. Paste a cookie below to download Instagram stories, private content, or anything a site hides from logged-out visitors. YouTube, TikTok and DeviantArt usually work without one.";
      summary.textContent = "Advanced · logins (needed for Instagram stories & most reels)";
    }
    if (caps.allow_user_cookies === false) {
      cookiesInput.disabled = true;
      cookiesInput.placeholder = "User-supplied cookies are disabled on this server.";
    }
    const enc = caps.ffmpeg ? caps.ffmpeg.encoder : "?";
    const hw = caps.ffmpeg && caps.ffmpeg.hardware ? " (hardware)" : "";
    const limit = caps.limits ? `${Math.round(caps.limits.max_duration_seconds / 60)} min` : "";
    const enhancement = caps.enhancement && caps.enhancement.enabled === false
      ? "enhancement off (downloads only)"
      : `encoder ${enc}${hw} · AI ${ai.available ? "ready" : "off"} · enhancement limit ${limit} per clip`;
    capsEl.textContent = `Server: yt-dlp ${caps.yt_dlp_version} · ${enhancement} · files auto-delete after ${caps.limits ? caps.limits.job_ttl_minutes : "?"} min.`;
    syncUrlHint();
  };

  const showStatus = (job) => {
    statusCard.classList.remove("hidden", "done", "error");
    stageEl.textContent = job.stage || job.status;
    const active = ["queued", "downloading", "enhancing"].includes(job.status);
    cancelBtn.classList.toggle("hidden", !active);
    submit.disabled = active;
    submit.textContent = active ? "Working…" : "Fetch";

    const pct = Math.round((job.progress || 0) * 100);
    const indeterminate = active && job.progress <= 0.001;
    barFill.classList.toggle("indeterminate", indeterminate);
    barFill.style.width = indeterminate ? "" : `${pct}%`;

    let sub = "";
    if (job.status === "downloading") sub = pct ? `${pct}%` : "Resolving the best rendition…";
    else if (job.status === "enhancing") sub = `${pct}% · interpolating and upscaling frame by frame — this takes a while`;
    else if (job.status === "done") sub = "Done. Files are kept for a limited time.";
    else if (job.status === "cancelled") sub = "Cancelled.";
    substageEl.textContent = sub;

    chipsEl.innerHTML = "";
    if (job.platform_name) chipsEl.appendChild(chip("site", job.platform_name));
    const src = job.sources && job.sources[0];
    if (src && src.is_image) {
      const who = src.channel ? `${src.channel}` : (src.uploader || "");
      if (who) chipsEl.appendChild(chip("by", who));
      chipsEl.appendChild(chip("source", `${src.width}×${src.height} image`));
    } else if (src) {
      const who = src.channel ? `@${src.channel}` : (src.uploader || "");
      if (who) chipsEl.appendChild(chip("by", who));
      chipsEl.appendChild(chip("source", `${src.width}×${src.height} @ ${fmtFps(src.fps)} fps`));
      chipsEl.appendChild(chip("length", fmtDuration(src.duration)));
      if (src.bit_rate) chipsEl.appendChild(chip("bitrate", `${(src.bit_rate / 1e6).toFixed(1)} Mbps`));
      if (job.sources.length > 1) chipsEl.appendChild(chip("items", String(job.sources.length)));
    }
    if (job.plan && job.options && job.options.mode === "enhance") {
      chipsEl.appendChild(chip("target", `${job.plan.target_width}×${job.plan.target_height} @ ${fmtFps(job.plan.target_fps)} fps`));
      if (job.engine) chipsEl.appendChild(chip("engine", job.engine));
    }
    if (job.auth) chipsEl.appendChild(chip("login", job.auth));

    errorEl.classList.toggle("hidden", !job.error);
    errorEl.textContent = job.error || "";
    warningsEl.innerHTML = "";
    warningsEl.classList.toggle("hidden", !(job.warnings && job.warnings.length));
    (job.warnings || []).forEach((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      warningsEl.appendChild(li);
    });

    if (job.status === "done") statusCard.classList.add("done");
    if (job.status === "error") statusCard.classList.add("error");
    elapsedEl.textContent = fmtElapsed(job.elapsed || 0);
  };

  const showResults = (job) => {
    resultsEl.innerHTML = "";
    if (!job.outputs || !job.outputs.length) { resultsEl.classList.add("hidden"); return; }
    resultsEl.classList.remove("hidden");
    job.outputs.forEach((out) => {
      const node = template.content.firstElementChild.cloneNode(true);
      node.classList.add(out.kind);
      const video = node.querySelector("video");
      const img = node.querySelector("img");
      if (out.is_image) {
        video.hidden = true;
        video.removeAttribute("src");
        img.hidden = false;
        img.src = `${out.url}?inline=1`;
        img.alt = out.download_name;
      } else {
        img.hidden = true;
        video.hidden = false;
        video.src = `${out.url}?inline=1`;
      }
      const platformName = job.platform_name || "the platform";
      node.querySelector(".result-kind").textContent = out.kind === "enhanced"
        ? `Enhanced · ${out.engine || ""}`
        : `Original · best rendition ${platformName} serves`;
      node.querySelector(".result-name").textContent = out.download_name;
      node.querySelector(".result-meta").textContent = out.is_image
        ? `${out.width}×${out.height} · image · ${fmtBytes(out.size)}`
        : `${out.width}×${out.height} · ${fmtFps(out.fps)} fps · ${fmtDuration(out.duration)} · ${fmtBytes(out.size)}` +
          (out.vcodec ? ` · ${out.vcodec}` : "");
      const link = node.querySelector(".download");
      link.href = out.url;
      link.setAttribute("download", out.download_name);
      link.textContent = out.kind === "enhanced" ? "Download enhanced" : "Download original";
      link.addEventListener("click", () => {
        // Embedded browsers save silently; confirm it and block accidental repeat clicks.
        const label = link.textContent;
        link.classList.add("busy");
        link.textContent = "Saving…";
        toast(`<b>Download started:</b> ${out.download_name} — look in your browser's Downloads folder.`);
        setTimeout(() => { link.classList.remove("busy"); link.textContent = label; }, 4000);
      });
      resultsEl.appendChild(node);
    });
  };

  // ------------------------------------------------------------------ job flow

  const stopPolling = () => {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  };

  const SERVER_DOWN = `Can't reach the server at ${location.origin}. Is it still running? Start it with \`uvicorn app.main:app\` and reload this page.`;
  let pollFailures = 0;

  const poll = async () => {
    if (!currentJob) return;
    try {
      const res = await fetch(`/api/jobs/${currentJob.id}`);
      if (res.status === 404) {
        showStatus({ ...currentJob, status: "error", stage: "Expired", error: "This job has expired." });
        stopPolling();
        return;
      }
      const job = await res.json();
      pollFailures = 0;
      currentJob = job;
      showStatus(job);
      if (["done", "error", "cancelled"].includes(job.status)) {
        showResults(job);
        stopPolling();
        return;
      }
    } catch (err) {
      console.warn("poll failed", err);
      pollFailures += 1;
      if (pollFailures >= 8) {
        showStatus({ ...currentJob, status: "error", stage: "Connection lost", error: SERVER_DOWN });
        stopPolling();
        return;
      }
    }
    const delay = currentJob && currentJob.status === "enhancing" ? 1500 : 800;
    pollTimer = setTimeout(poll, delay);
  };

  const startJob = async (payload) => {
    stopPolling();
    resultsEl.classList.add("hidden");
    resultsEl.innerHTML = "";
    showStatus({ status: "queued", stage: "Submitting…", progress: 0, elapsed: 0 });
    let res;
    try {
      res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (err) {
      console.warn("submit failed", err);
      showStatus({ status: "error", stage: "Server unreachable", error: SERVER_DOWN, progress: 0, elapsed: 0 });
      return;
    }
    if (!res.ok) {
      let detail = `Request failed (${res.status})`;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch { /* ignore */ }
      showStatus({ status: "error", stage: "Rejected", error: detail, progress: 0, elapsed: 0 });
      return;
    }
    currentJob = await res.json();
    pollFailures = 0;
    const started = Date.now();
    showStatus(currentJob);
    tickTimer = setInterval(() => {
      if (currentJob && ["queued", "downloading", "enhancing"].includes(currentJob.status)) {
        elapsedEl.textContent = fmtElapsed((Date.now() - started) / 1000);
      }
    }, 1000);
    poll();
  };

  const submitCurrent = () => {
    const url = urlInput.value.trim();
    if (!url || submit.disabled) return;
    saveOptions();
    persistCookies();
    const payload = {
      url,
      mode: form.elements.mode.value,
      resolution: form.elements.resolution.value,
      fps: form.elements.fps.value,
      engine: form.elements.engine.value,
    };
    const cookies = cookiesInput.value.trim();
    if (cookies) payload.cookies = cookies;
    startJob(payload);
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCurrent();
  });

  // Paste-to-go: an Instagram link pasted anywhere (except into another field) starts the job.
  const handlePaste = (event) => {
    const target = event.target;
    const isOtherField = target && target !== urlInput &&
      (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.isContentEditable);
    if (isOtherField) return;
    const text = event.clipboardData ? event.clipboardData.getData("text") : "";
    const url = extractUrl(text);
    if (!url) return;
    event.preventDefault();
    urlInput.value = url;
    urlInput.focus();
    syncUrlHint();
    submitCurrent();
  };
  document.addEventListener("paste", handlePaste);

  cancelBtn.addEventListener("click", async () => {
    if (!currentJob) return;
    cancelBtn.disabled = true;
    try { await fetch(`/api/jobs/${currentJob.id}`, { method: "DELETE" }); } finally { cancelBtn.disabled = false; }
  });

  form.elements.mode.forEach((radio) => radio.addEventListener("change", () => { syncModeUI(); saveOptions(); }));
  ["resolution", "fps", "engine"].forEach((name) => form.elements[name].addEventListener("change", saveOptions));
  urlInput.addEventListener("input", syncUrlHint);
  cookiesInput.addEventListener("input", syncUrlHint);
  rememberCookies.addEventListener("change", persistCookies);

  // ------------------------------------------------------------------ boot

  loadOptions();
  syncModeUI();
  fetch("/api/capabilities")
    .then((r) => r.json())
    .then(applyCapabilities)
    .catch(() => {
      capsEl.textContent = SERVER_DOWN;
      capsEl.classList.add("warn");
    });
  const params = new URLSearchParams(location.search);
  if (params.get("url")) {
    urlInput.value = params.get("url");
    syncUrlHint();
  }
})();
