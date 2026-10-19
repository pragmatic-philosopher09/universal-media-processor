(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const form = $("#job-form");
  const urlInput = $("#url");
  const urlHint = $("#url-hint");
  const addLinkBtn = $("#add-link");
  const dropzone = $("#dropzone");
  const dropzoneHint = $("#dropzone-hint");
  const fileInput = $("#file-input");
  const queueWrap = $("#queue-wrap");
  const queueEl = $("#queue");
  const queueCount = $("#queue-count");
  const queueClear = $("#queue-clear");
  const submit = $("#submit");
  const submitHint = $("#submit-hint");
  const enhanceOptions = $("#enhance-options");
  const standardizeOptions = $("#standardize-options");
  const enhanceOffNote = $("#enhance-off-note");
  const reviewToggle = $("#review");
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
  const itemsEl = $("#items");
  const errorEl = $("#error");
  const warningsEl = $("#warnings");

  const reviewCard = $("#review-card");
  const proposalsEl = $("#proposals");
  const approveBtn = $("#approve");
  const reviewCancelBtn = $("#review-cancel");
  const reviewSummary = $("#review-summary");

  const resultsWrap = $("#results-wrap");
  const resultsEl = $("#results");
  const downloadAll = $("#download-all");
  const manifestJson = $("#manifest-json");
  const manifestCsv = $("#manifest-csv");
  const capsEl = $("#caps");
  const template = $("#result-template");

  const STORAGE = { options: "reeldl.options", cookies: "reeldl.cookies" };
  const ACTIVE = ["queued", "downloading", "review", "enhancing"];
  const FINISHED = ["done", "error", "cancelled"];
  let capabilities = null;
  let currentJob = null;
  let pollTimer = null;
  let tickTimer = null;
  let reviewShownFor = null;
  let planRequest = 0;

  /** Batch queue: links and uploaded files waiting to be submitted together. */
  const queue = [];
  let queueSeq = 0;

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
  const fmtFps = (fps) => (fps == null ? "?" : Math.abs(fps - Math.round(fps)) < 0.05 ? String(Math.round(fps)) : fps.toFixed(2));
  const fmtElapsed = (seconds) => {
    const s = Math.max(0, Math.round(seconds));
    const m = Math.floor(s / 60);
    return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  };
  const fmtFrame = (m) => (m.is_image ? `${m.width}×${m.height} image` : `${m.width}×${m.height} @ ${fmtFps(m.fps)} fps`);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };

  let toastTimer = null;
  const toast = (html) => {
    let node = document.querySelector(".toast");
    if (!node) { node = el("div", "toast"); node.setAttribute("role", "status"); document.body.appendChild(node); }
    node.innerHTML = html;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 6000);
  };
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const chip = (label, value) => {
    const span = el("span", "chip", `${label} `);
    span.appendChild(el("b", null, value));
    return span;
  };

  const TREATMENTS = {
    native: ["Native", "native"],
    uploaded: ["Your file", "native"],
    upscaled: ["Upscaled", "gen"],
    sharpened: ["Sharpened", "gen"],
    interpolated: ["Interpolated", "gen"],
    ai_upscaled: ["AI upscaled", "gen"],
    ai_interpolated: ["AI interpolated", "gen"],
    downscaled: ["Downscaled", "fit"],
    letterboxed: ["Letterboxed", "fit"],
    cropped: ["Cropped", "fit"],
    fps_converted: ["Frame rate converted", "fit"],
    constant_fps: ["Constant frame rate", "fit"],
    transcoded: ["Transcoded", "fit"],
    converted_png: ["Converted to PNG", "fit"],
  };
  const badges = (codes) => {
    const wrap = el("div", "badges");
    (codes || []).forEach((code) => {
      const [label, tone] = TREATMENTS[code] || [code, "fit"];
      const badge = el("span", `badge ${tone}`, label);
      badge.title = tone === "gen" ? "New pixels or frames were generated" : tone === "native" ? "Untouched source" : "Conformed for editing";
      wrap.appendChild(badge);
    });
    return wrap;
  };

  const URL_RE = /(?:https?:\/\/)?(?:[\w-]+\.)*(?:instagram\.com|instagr\.am|ig\.me|youtube\.com|youtu\.be|youtube-nocookie\.com|tiktok\.com|deviantart\.com|fav\.me)\/\S+/gi;
  const clean = (url) => url.replace(/[.,;:!?)"']+$/, "");
  const extractUrls = (text) => [...new Set(((text || "").match(URL_RE) || []).map(clean))];
  const extractUrl = (text) => extractUrls(text)[0] || null;

  const platformOf = (value) => {
    if (/youtube\.com|youtu\.be/i.test(value)) return "youtube";
    if (/tiktok\.com/i.test(value)) return "tiktok";
    if (/deviantart\.com|fav\.me/i.test(value)) return "deviantart";
    if (/instagram\.com|instagr\.am|ig\.me/i.test(value)) return "instagram";
    return null;
  };
  const PLATFORM_NAMES = { youtube: "YouTube", tiktok: "TikTok", deviantart: "DeviantArt", instagram: "Instagram" };

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

  // ------------------------------------------------------------------ options

  const mode = () => form.elements.mode.value;
  const processingOptions = () => ({
    mode: mode(),
    resolution: form.elements.resolution.value,
    fps: form.elements.fps.value,
    engine: form.elements.engine.value,
    standardize: {
      canvas: form.elements.std_canvas.value,
      fps: form.elements.std_fps.value,
      fit: form.elements.std_fit.value,
    },
  });

  const saveOptions = () => {
    const o = processingOptions();
    const data = {
      mode: o.mode, resolution: o.resolution, fps: o.fps, engine: o.engine,
      standardize: o.standardize, review: reviewToggle.checked,
    };
    localStorage.setItem(STORAGE.options, JSON.stringify(data));
  };
  const setSelect = (select, value) => {
    if (value && [...select.options].some((o) => o.value === value && !o.disabled)) select.value = value;
  };
  const loadOptions = () => {
    try {
      const data = JSON.parse(localStorage.getItem(STORAGE.options) || "{}");
      if (data.mode) form.elements.mode.value = data.mode;
      setSelect(form.elements.resolution, data.resolution);
      setSelect(form.elements.fps, data.fps);
      setSelect(form.elements.engine, data.engine);
      if (data.standardize) {
        setSelect(form.elements.std_canvas, data.standardize.canvas);
        setSelect(form.elements.std_fps, data.standardize.fps);
        setSelect(form.elements.std_fit, data.standardize.fit);
      }
      if (typeof data.review === "boolean") reviewToggle.checked = data.review;
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
    const m = mode();
    enhanceOptions.classList.toggle("hidden", m !== "enhance");
    standardizeOptions.classList.toggle("hidden", m !== "standardize");
    form.querySelectorAll(".outcome").forEach((card) => {
      card.classList.toggle("selected", card.dataset.mode === m);
    });
  };

  const PASTE_TIP = "Tip: paste anywhere on this page (⌘V / Ctrl+V) and the download starts immediately.";
  const QUEUE_TIP = "Pasted links join the batch below. Press Enter or “Add to batch” to add the typed link.";
  const browserLoginActive = () => !!(capabilities && capabilities.browser_login && capabilities.browser_login.active_for_you);
  const maxItems = () => (capabilities && capabilities.limits && capabilities.limits.max_items_per_job) || 10;
  const uploadsEnabled = () => !(capabilities && capabilities.uploads && capabilities.uploads.enabled === false);

  const syncUrlHint = () => {
    const kind = classifyUrl(urlInput.value.trim());
    urlHint.className = "hint";
    if (!kind) { urlHint.textContent = queue.length ? QUEUE_TIP : PASTE_TIP; return; }
    if (kind === "story" || kind === "highlight") {
      const configured = capabilities && capabilities.stories_auth_configured;
      const pasted = cookiesInput.value.trim().length > 0;
      if (configured || pasted) {
        urlHint.textContent = "Story link — login is configured, you're good to go.";
        urlHint.classList.add("ok");
      } else if (kind === "story" && capabilities?.instagram_public_media?.stories) {
        urlHint.textContent = "Public stories are fetched through FastVideoSave. Use a story link or instagram.com/stories/username/. Expired or private stories may be unavailable.";
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

  const jobActive = () => !!(currentJob && ACTIVE.includes(currentJob.status));

  /** Primary button label and enablement follow the batch and the chosen outcome. */
  const syncSubmit = () => {
    const typed = urlInput.value.trim();
    const pendingTyped = typed && !queue.some((q) => q.type === "link" && q.url === typed) ? 1 : 0;
    const count = queue.length + pendingTyped;
    const uploading = queue.some((q) => q.type === "upload" && q.status === "uploading");
    const failed = queue.some((q) => q.type === "upload" && q.status === "error");
    const tooMany = count > maxItems();
    const verb = { original: "Fetch", enhance: "Enhance", standardize: "Standardize" }[mode()] || "Fetch";

    if (jobActive()) {
      submit.disabled = true;
      submit.textContent = currentJob.status === "review" ? "Waiting for review…" : "Working…";
      submitHint.textContent = "";
      return;
    }
    submit.disabled = count === 0 || uploading || tooMany;
    if (!queue.length) {
      submit.textContent = verb === "Fetch" ? "Fetch" : `Fetch & ${verb.toLowerCase()}`;
    } else {
      submit.textContent = `${reviewToggle.checked ? "Review" : verb} ${plural(count, "item")}`;
    }
    submitHint.className = "hint";
    if (tooMany) {
      submitHint.textContent = `This server takes up to ${maxItems()} items per batch.`;
      submitHint.classList.add("warn");
    } else if (uploading) {
      submitHint.textContent = "Waiting for uploads to finish…";
    } else if (failed) {
      submitHint.textContent = "Failed uploads are skipped.";
      submitHint.classList.add("warn");
    } else if (count && reviewToggle.checked && queue.length) {
      submitHint.textContent = "You'll see exactly what will happen to each file before anything is processed.";
    } else {
      submitHint.textContent = "";
    }
  };

  // ------------------------------------------------------------------ queue

  const renderQueue = () => {
    queueEl.innerHTML = "";
    queueWrap.classList.toggle("hidden", queue.length === 0);
    queueCount.textContent = `Batch · ${plural(queue.length, "item")}`;
    queue.forEach((entry) => {
      const li = el("li", `queue-item ${entry.type} ${entry.status || ""}`);
      const icon = el("span", "q-icon", entry.type === "link" ? "↗" : entry.status === "error" ? "!" : "▣");
      const body = el("div", "q-body");
      const name = el("span", "q-name", entry.type === "link" ? entry.url : entry.filename);
      name.title = name.textContent;
      const meta = el("span", "q-meta");
      if (entry.type === "link") {
        meta.textContent = PLATFORM_NAMES[entry.platform] || "Link";
      } else if (entry.status === "uploading") {
        meta.textContent = `Uploading ${Math.round(entry.progress * 100)}% of ${fmtBytes(entry.size)}`;
      } else if (entry.status === "error") {
        meta.textContent = entry.error || "Upload failed";
      } else {
        const info = entry.info || {};
        meta.textContent = `Your file · ${fmtBytes(entry.size)}` + (info.width ? ` · ${fmtFrame(info)}` : "") +
          (info.duration && !info.is_image ? ` · ${fmtDuration(info.duration)}` : "");
      }
      body.append(name, meta);
      if (entry.status === "uploading") {
        const bar = el("div", "q-bar");
        const fill = el("div", "q-bar-fill");
        fill.style.width = `${Math.round(entry.progress * 100)}%`;
        bar.appendChild(fill);
        body.appendChild(bar);
      }
      const remove = el("button", "q-remove", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${name.textContent}`);
      remove.addEventListener("click", () => removeEntry(entry));
      li.append(icon, body, remove);
      queueEl.appendChild(li);
    });
    syncUrlHint();
    syncSubmit();
  };

  const addLinks = (urls) => {
    let added = 0;
    urls.forEach((url) => {
      if (!url || queue.some((q) => q.type === "link" && q.url === url)) return;
      queue.push({ key: ++queueSeq, type: "link", url, platform: platformOf(url) });
      added += 1;
    });
    if (added) renderQueue();
    return added;
  };

  const uploadFile = (file) => {
    const entry = { key: ++queueSeq, type: "upload", filename: file.name, size: file.size, status: "uploading", progress: 0 };
    const limitMb = capabilities && capabilities.uploads ? capabilities.uploads.max_mb : null;
    if (limitMb && file.size > limitMb * 1024 * 1024) {
      entry.status = "error";
      entry.error = `Too large — this server accepts up to ${limitMb} MB per file.`;
      queue.push(entry);
      renderQueue();
      return;
    }
    queue.push(entry);
    const xhr = new XMLHttpRequest();
    entry.xhr = xhr;
    xhr.open("POST", `/api/uploads?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    let lastPaint = 0;
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      entry.progress = event.loaded / event.total;
      const now = Date.now();
      if (now - lastPaint > 150) { lastPaint = now; renderQueue(); }
    });
    xhr.addEventListener("load", () => {
      entry.xhr = null;
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status === 201 && body) {
        Object.assign(entry, { status: "ready", id: body.id, size: body.size, info: body.info, progress: 1 });
      } else {
        entry.status = "error";
        entry.error = (body && typeof body.detail === "string" && body.detail) || `Upload failed (${xhr.status})`;
      }
      renderQueue();
    });
    const fail = (message) => () => { entry.xhr = null; entry.status = "error"; entry.error = message; renderQueue(); };
    xhr.addEventListener("error", fail("Upload failed — is the server still running?"));
    xhr.addEventListener("abort", fail("Upload cancelled"));
    xhr.send(file);
    renderQueue();
  };

  const removeEntry = (entry) => {
    const i = queue.indexOf(entry);
    if (i >= 0) queue.splice(i, 1);
    if (entry.xhr) { entry.xhr.abort(); entry.xhr = null; }
    if (entry.type === "upload" && entry.id) {
      fetch(`/api/uploads/${entry.id}`, { method: "DELETE" }).catch(() => {});
    }
    renderQueue();
  };

  const clearQueue = ({ discardUploads }) => {
    queue.splice(0).forEach((entry) => {
      if (entry.xhr) entry.xhr.abort();
      if (discardUploads && entry.type === "upload" && entry.id) {
        fetch(`/api/uploads/${entry.id}`, { method: "DELETE" }).catch(() => {});
      }
    });
    renderQueue();
  };

  const addFiles = (files) => {
    if (!uploadsEnabled()) { toast("File uploads are disabled on this server."); return; }
    [...files].forEach(uploadFile);
  };

  // ------------------------------------------------------------------ capabilities

  const applyCapabilities = (caps) => {
    capabilities = caps;
    (caps.outcomes || []).forEach((outcome) => {
      const card = form.querySelector(`.outcome[data-mode="${outcome.id}"]`);
      if (!card) return;
      card.querySelector(".outcome-name").textContent = outcome.name;
      card.querySelector(".outcome-desc").textContent = outcome.description;
      card.querySelector(".outcome-trade").textContent = outcome.tradeoff;
      if (outcome.available === false) {
        const radio = card.querySelector("input");
        radio.disabled = true;
        card.classList.add("disabled");
        card.querySelector(".outcome-trade").textContent = "Off on this server";
      }
    });
    if (caps.enhancement && caps.enhancement.enabled === false) {
      const enhanceRadio = form.querySelector('input[name="mode"][value="enhance"]');
      enhanceRadio.disabled = true;
      enhanceRadio.closest(".outcome").title = caps.enhancement.reason || "Enhancement is disabled on this server.";
      enhanceOffNote.textContent = "This server only downloads and standardizes — it doesn't have the CPU for 4K 60 fps rendering. Run the app locally or on a real VM for Enhance.";
      enhanceOffNote.classList.remove("hidden");
      if (mode() === "enhance" || !mode()) form.elements.mode.value = "original";
    }
    syncModeUI();

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

    if (caps.uploads && caps.uploads.enabled === false) {
      dropzone.classList.add("disabled");
      dropzone.querySelector(".dropzone-title").textContent = "File uploads are off on this server";
      dropzoneHint.textContent = "Links still work.";
      fileInput.disabled = true;
    } else if (caps.uploads) {
      dropzoneHint.textContent = `MP4, MOV, MKV, WebM, AVI, PNG, JPG, WebP · up to ${caps.uploads.max_mb} MB each · up to ${maxItems()} items per batch.`;
    }

    const summary = $("#advanced-summary");
    if (caps.stories_auth_configured) {
      authStatus.className = "hint ok";
      authStatus.textContent = "This server already has an Instagram session configured — stories work without pasting anything. You can still paste your own cookie to use your account instead.";
      summary.textContent = "Advanced · logins (Instagram configured on the server)";
    } else if (browserLoginActive()) {
      authStatus.className = "hint ok";
      const browsers = (caps.browser_login.browsers || []).slice(0, 3).map((b) => b[0].toUpperCase() + b.slice(1)).join(", ");
      authStatus.textContent = caps.browser_login.mode === "always"
        ? `Instagram downloads can use the server owner's browser login (${browsers}…) automatically. Everyone with access to this app shares that Instagram session. Keep the app password private.`
        : `You're on the computer running this server, so whenever a site demands a login the app uses the session from your own browser (${browsers}…) automatically — nothing to paste. Only fill the box below if that fails (e.g. Safari needs Full Disk Access to be readable).`;
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
      ? "enhancement off (downloads and standardize only)"
      : `encoder ${enc}${hw} · AI ${ai.available ? "ready" : "off"} · enhancement limit ${limit} per clip`;
    const product = caps.product ? `${caps.product.name} ${caps.product.version} · ` : "";
    capsEl.textContent = `${product}yt-dlp ${caps.yt_dlp_version} · ${enhancement} · files auto-delete after ${caps.limits ? caps.limits.job_ttl_minutes : "?"} min.`;
    syncUrlHint();
    syncSubmit();
  };

  // ------------------------------------------------------------------ status

  const INPUT_STATUS = { pending: "Waiting", fetching: "Fetching…", ready: "Ready", failed: "Failed" };

  const renderItems = (job) => {
    const inputs = job.inputs || [];
    const show = inputs.length > 1 || inputs.some((i) => i.kind === "upload");
    itemsEl.classList.toggle("hidden", !show);
    itemsEl.innerHTML = "";
    if (!show) return;
    inputs.forEach((input) => {
      const li = el("li", `item ${input.status}`);
      if (job.current_item === input.index + 1 && ACTIVE.includes(job.status) && job.status !== "review") li.classList.add("current");
      const label = el("span", "item-label", input.label);
      label.title = input.label;
      const meta = el("span", "item-meta");
      const parts = [input.platform_name, INPUT_STATUS[input.status] || input.status];
      if (input.item_count > 1) parts.push(plural(input.item_count, "item"));
      if (input.auth) parts.push(`login: ${input.auth}`);
      meta.textContent = parts.join(" · ");
      li.append(el("span", "item-dot"), label, meta);
      if (input.error) li.appendChild(el("span", "item-error", input.error));
      itemsEl.appendChild(li);
    });
  };

  const showStatus = (job) => {
    statusCard.classList.remove("hidden", "done", "error");
    stageEl.textContent = job.stage || job.status;
    const active = ACTIVE.includes(job.status);
    cancelBtn.classList.toggle("hidden", !active || job.status === "review");

    const pct = Math.round((job.progress || 0) * 100);
    const indeterminate = active && job.status !== "review" && job.progress <= 0.001;
    barFill.classList.toggle("indeterminate", indeterminate);
    barFill.style.width = indeterminate ? "" : `${job.status === "review" ? 100 : pct}%`;
    statusCard.classList.toggle("paused", job.status === "review");

    const many = (job.item_count || 0) > 1;
    const position = many && job.current_item ? `Item ${job.current_item} of ${job.item_count} · ` : "";
    const processing = job.options && job.options.mode === "standardize"
      ? "conforming frame size, frame rate and codecs"
      : "interpolating and upscaling frame by frame — this takes a while";
    let sub = "";
    if (job.status === "downloading") sub = position + (pct ? `${pct}%` : "Resolving the best rendition…");
    else if (job.status === "review") sub = "Waiting for you — check the plan below.";
    else if (job.status === "enhancing") sub = `${position}${pct}% · ${processing}`;
    else if (job.status === "done") sub = "Done. Files are kept for a limited time.";
    else if (job.status === "cancelled") sub = "Cancelled.";
    substageEl.textContent = sub;

    chipsEl.innerHTML = "";
    if (job.platform_name) chipsEl.appendChild(chip("source", job.platform_name));
    const sources = job.sources || [];
    const src = sources[0];
    if (sources.length === 1 && src) {
      const who = src.channel ? (src.is_image ? src.channel : `@${src.channel}`) : (src.uploader || "");
      if (who) chipsEl.appendChild(chip("by", who));
      chipsEl.appendChild(chip("media", fmtFrame(src)));
      if (!src.is_image) chipsEl.appendChild(chip("length", fmtDuration(src.duration)));
      if (src.bit_rate) chipsEl.appendChild(chip("bitrate", `${(src.bit_rate / 1e6).toFixed(1)} Mbps`));
    } else if (sources.length > 1) {
      chipsEl.appendChild(chip("items", String(sources.length)));
    }
    const outcome = job.options && job.options.mode;
    if (outcome) {
      const name = { original: "Keep original", enhance: "Enhance", standardize: "Standardize" }[outcome] || outcome;
      chipsEl.appendChild(chip("outcome", name));
    }
    if (job.plan && outcome === "enhance") {
      chipsEl.appendChild(chip("target", `${job.plan.target_width}×${job.plan.target_height} @ ${fmtFps(job.plan.target_fps)} fps`));
      if (job.engine) chipsEl.appendChild(chip("engine", job.engine));
    }
    if (outcome === "standardize" && job.options.standardize) {
      const s = job.options.standardize;
      const size = s.canvas === "source" ? "source size" : s.canvas.replace("x", "×");
      chipsEl.appendChild(chip("target", `${size} @ ${s.fps === "source" ? "source" : s.fps} fps · ${s.fit === "crop" ? "fill" : "fit"}`));
    }
    if (job.auth) chipsEl.appendChild(chip("login", job.auth));

    renderItems(job);

    errorEl.classList.toggle("hidden", !job.error);
    errorEl.textContent = job.error || "";
    warningsEl.innerHTML = "";
    warningsEl.classList.toggle("hidden", !(job.warnings && job.warnings.length));
    (job.warnings || []).forEach((w) => warningsEl.appendChild(el("li", null, w)));

    if (job.status === "done") statusCard.classList.add("done");
    if (job.status === "error") statusCard.classList.add("error");
    elapsedEl.textContent = fmtElapsed(job.elapsed || 0);
    syncSubmit();
  };

  // ------------------------------------------------------------------ review

  const describeTarget = (p) => {
    if (p.action !== "process") return "Kept as is";
    const t = p.target || {};
    return p.source.is_image ? `${t.width}×${t.height} image` : `${t.width}×${t.height} @ ${fmtFps(t.fps)} fps`;
  };

  const renderProposals = (job, proposals) => {
    proposalsEl.innerHTML = "";
    const inputs = job.inputs || [];
    let processing = 0;
    let generated = 0;
    (proposals || []).forEach((p) => {
      if (p.action === "process") processing += 1;
      if ((p.treatments || []).some((t) => (TREATMENTS[t] || [])[1] === "gen")) generated += 1;
      const li = el("li", `proposal ${p.action}`);
      const head = el("div", "proposal-head");
      const name = el("span", "proposal-name", p.name);
      name.title = p.name;
      const from = inputs[p.input_index];
      head.append(name);
      if (from && inputs.length > 1) head.appendChild(el("span", "proposal-from", from.platform_name));
      const flow = el("div", "proposal-flow");
      flow.append(
        el("span", "frame", fmtFrame(p.source) + (!p.source.is_image && p.source.duration ? ` · ${fmtDuration(p.source.duration)}` : "")),
        el("span", "arrow", "→"),
        el("span", `frame target ${p.action}`, describeTarget(p) + (p.engine ? ` · ${p.engine}` : "")),
      );
      li.append(head, flow, badges(p.treatments));
      if (p.warnings && p.warnings.length) {
        const warn = el("ul", "proposal-warnings");
        p.warnings.forEach((w) => warn.appendChild(el("li", null, w)));
        li.appendChild(warn);
      }
      proposalsEl.appendChild(li);
    });
    const total = (proposals || []).length;
    const parts = [plural(total, "file")];
    parts.push(processing ? `${processing} will be processed` : "all kept as they are");
    if (generated) parts.push(`${generated} with generated pixels or frames`);
    reviewSummary.textContent = parts.join(" · ");
    approveBtn.textContent = processing ? `Process ${plural(total, "file")}` : `Get ${plural(total, "file")}`;
  };

  const showReview = (job) => {
    reviewCard.classList.remove("hidden");
    if (reviewShownFor !== job.id) {
      reviewShownFor = job.id;
      approveBtn.disabled = false;
      renderProposals(job, job.proposals);
      reviewCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
      // The form may have changed since submission; show the plan for what's selected now.
      const sent = job.options || {};
      const now = processingOptions();
      if (sent.mode !== now.mode || JSON.stringify(sent.standardize) !== JSON.stringify(now.standardize) ||
          sent.resolution !== now.resolution || sent.fps !== now.fps || sent.engine !== now.engine) {
        replan();
      }
    }
  };
  const hideReview = () => {
    reviewCard.classList.add("hidden");
    proposalsEl.innerHTML = "";
  };

  const apiError = async (res) => {
    try {
      const body = await res.json();
      return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { return `Request failed (${res.status})`; }
  };

  const replan = async () => {
    if (!currentJob || currentJob.status !== "review") return;
    const ticket = ++planRequest;
    reviewCard.classList.add("loading");
    try {
      const res = await fetch(`/api/jobs/${currentJob.id}/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(processingOptions()),
      });
      if (ticket !== planRequest) return;
      if (!res.ok) { reviewSummary.textContent = await apiError(res); return; }
      const body = await res.json();
      renderProposals(currentJob, body.proposals);
    } catch (err) {
      console.warn("plan failed", err);
    } finally {
      if (ticket === planRequest) reviewCard.classList.remove("loading");
    }
  };

  approveBtn.addEventListener("click", async () => {
    if (!currentJob) return;
    approveBtn.disabled = true;
    saveOptions();
    try {
      const res = await fetch(`/api/jobs/${currentJob.id}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(processingOptions()),
      });
      if (!res.ok) {
        reviewSummary.textContent = await apiError(res);
        approveBtn.disabled = false;
        return;
      }
      currentJob = await res.json();
      hideReview();
      showStatus(currentJob);
      clearTimeout(pollTimer);
      poll();
    } catch (err) {
      console.warn("start failed", err);
      approveBtn.disabled = false;
    }
  });

  // ------------------------------------------------------------------ results

  const kindLabel = (out, job) => {
    if (out.kind === "converted") return `Converted MP4 · ${out.engine || "ffmpeg"}`;
    if (out.kind === "enhanced") return `${out.is_image ? "Upscaled" : "Enhanced"} · ${out.engine || ""}`;
    if (out.kind === "standardized") return "Standardized for editing";
    if ((out.treatments || []).includes("uploaded")) return "Your file · untouched";
    const input = (job.inputs || [])[out.input_index];
    const platformName = (input && input.platform_name) || job.platform_name || "the platform";
    if (job.source_provider) return `Original · supplied by ${job.source_provider}`;
    return `Original · best rendition ${platformName} serves`;
  };

  const showResults = (job) => {
    resultsEl.innerHTML = "";
    const outputs = job.outputs || [];
    if (!outputs.length) { resultsWrap.classList.add("hidden"); return; }
    resultsWrap.classList.remove("hidden");
    const done = job.status === "done";
    downloadAll.classList.toggle("hidden", !done || outputs.length < 2);
    downloadAll.href = `/api/jobs/${job.id}/archive`;
    downloadAll.setAttribute("download", `mediaflow-${job.id}.zip`);
    downloadAll.textContent = `Download all · ${outputs.length} files (.zip)`;
    manifestJson.href = `/api/jobs/${job.id}/manifest?format=json`;
    manifestCsv.href = `/api/jobs/${job.id}/manifest?format=csv`;
    manifestJson.classList.toggle("hidden", !done);
    manifestCsv.classList.toggle("hidden", !done);

    outputs.forEach((out) => resultsEl.appendChild(renderOutput(out, job)));
  };

  /** Poll a single-file follow-up job (convert / upscale) and show its file under the original. */
  const runFollowUp = async (button, note, url, node) => {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Starting…";
    note.textContent = "";
    try {
      const res = await fetch(url, { method: "POST" });
      if (!res.ok) throw new Error(await apiError(res));
      let job = await res.json();
      while (!["done", "error", "cancelled"].includes(job.status)) {
        const pct = Math.round((job.progress || 0) * 100);
        button.textContent = job.status === "queued" ? "Queued…" : `Working · ${pct}%`;
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const poll = await fetch(`/api/jobs/${job.id}`);
        if (!poll.ok) throw new Error("The conversion job expired. Try again.");
        job = await poll.json();
      }
      if (job.status !== "done") throw new Error(job.error || "Cancelled.");
      [...job.outputs].reverse().forEach((out) => node.after(renderOutput(out, job)));
      button.textContent = "Done — see below";
    } catch (err) {
      button.disabled = false;
      button.textContent = label;
      note.textContent = err.message || String(err);
    }
  };

  const renderOutput = (out, job) => {
    const multiInput = (job.inputs || []).length > 1;
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
    node.querySelector(".result-kind").textContent = kindLabel(out, job);
    node.querySelector(".result-name").textContent = out.download_name;
    const input = (job.inputs || [])[out.input_index];
    node.querySelector(".result-meta").textContent = (out.is_image
      ? `${out.width}×${out.height} · image · ${fmtBytes(out.size)}`
      : `${out.width}×${out.height} · ${fmtFps(out.fps)} fps · ${fmtDuration(out.duration)} · ${fmtBytes(out.size)}` +
        (out.vcodec ? ` · ${out.vcodec}` : "") +
        (out.has_audio === true ? " · audio" : out.has_audio === false ? " · no audio" : "")) +
      (multiInput && input ? ` · from ${input.platform_name}` : "");
    node.querySelector(".badges").replaceWith(badges(out.treatments));
    const link = node.querySelector(".download");
    link.href = out.url;
    link.setAttribute("download", out.download_name);
    link.textContent = out.kind === "converted" ? (job.conversion_preset === "2160p60" ? "Download 4K 60 fps" : "Download MP4")
      : out.kind === "enhanced" ? (out.is_image ? "Download upscaled PNG" : "Download enhanced")
      : out.kind === "standardized" ? "Download standardized"
      : out.is_image ? "Download photo" : "Download original";
    link.addEventListener("click", () => {
      // Embedded browsers save silently; confirm it and block accidental repeat clicks.
      const label = link.textContent;
      link.classList.add("busy");
      link.textContent = "Saving…";
      toast(`<b>Download started:</b> ${escapeHtml(out.download_name)} — look in your browser's Downloads folder.`);
      setTimeout(() => { link.classList.remove("busy"); link.textContent = label; }, 4000);
    });
    const conversionEnabled = !!(capabilities && capabilities.conversion && capabilities.conversion.enabled);
    if (out.kind === "original" && job.status === "done" && conversionEnabled) {
      const box = node.querySelector(".result-conversion");
      const button = box.querySelector(".upscale");
      const note = box.querySelector(".conversion-availability");
      box.classList.remove("hidden");
      if (out.is_image) {
        button.textContent = "Upscale image to 4K";
        box.querySelector(".hint").textContent = "Upscale this photo to a 4K-size PNG without fetching it again.";
      }
      button.addEventListener("click", () => {
        if (button.disabled) return;
        runFollowUp(button, note, out.is_image
          ? `${out.url}/upscale?resolution=2160p`
          : `${out.url}/convert?preset=2160p60`, node);
      });
    }
    return node;
  };

  [downloadAll, manifestJson, manifestCsv].forEach((link) => link.addEventListener("click", () => {
    toast(`<b>Download started:</b> ${escapeHtml(link.textContent)} — look in your browser's Downloads folder.`);
  }));

  // ------------------------------------------------------------------ job flow

  const stopPolling = () => {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  };

  const SERVER_DOWN = `Can't reach the server at ${location.origin}. Is it still running? Start it with \`uvicorn app.main:app\` and reload this page.`;
  let pollFailures = 0;

  const finish = (job) => {
    hideReview();
    showResults(job);
    stopPolling();
    syncSubmit();
  };

  const poll = async () => {
    if (!currentJob) return;
    try {
      const res = await fetch(`/api/jobs/${currentJob.id}`);
      if (res.status === 404) {
        currentJob = { ...currentJob, status: "error", stage: "Expired", error: "This job has expired." };
        showStatus(currentJob);
        finish(currentJob);
        return;
      }
      if (!res.ok) throw new Error(`Status request failed (${res.status})`);
      const job = await res.json();
      pollFailures = 0;
      currentJob = job;
      showStatus(job);
      if (job.status === "review") showReview(job);
      else hideReview();
      if (FINISHED.includes(job.status)) { finish(job); return; }
    } catch (err) {
      console.warn("poll failed", err);
      pollFailures += 1;
      if (pollFailures >= 8) {
        currentJob = { ...currentJob, status: "error", stage: "Connection lost", error: SERVER_DOWN };
        showStatus(currentJob);
        finish(currentJob);
        return;
      }
    }
    const status = currentJob && currentJob.status;
    const delay = status === "review" ? 3000 : status === "enhancing" ? 1500 : 800;
    pollTimer = setTimeout(poll, delay);
  };

  const startJob = async (payload, { onAccepted } = {}) => {
    stopPolling();
    hideReview();
    reviewShownFor = null;
    resultsWrap.classList.add("hidden");
    resultsEl.innerHTML = "";
    currentJob = null;
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
      showStatus({ status: "error", stage: "Rejected", error: await apiError(res), progress: 0, elapsed: 0 });
      return;
    }
    currentJob = await res.json();
    if (onAccepted) onAccepted();
    pollFailures = 0;
    const started = Date.now();
    showStatus(currentJob);
    tickTimer = setInterval(() => {
      if (currentJob && ACTIVE.includes(currentJob.status)) {
        elapsedEl.textContent = fmtElapsed((Date.now() - started) / 1000);
      }
    }, 1000);
    poll();
  };

  const basePayload = () => {
    const payload = processingOptions();
    const cookies = cookiesInput.value.trim();
    if (cookies) payload.cookies = cookies;
    return payload;
  };

  /** v1 fast path: one link, straight through, no review step. */
  const submitSingle = (url, { review = false } = {}) => {
    if (!url || jobActive()) return;
    saveOptions();
    persistCookies();
    startJob({ url, ...basePayload(), review });
  };

  const submitBatch = () => {
    if (jobActive()) return;
    const typed = extractUrl(urlInput.value.trim()) || urlInput.value.trim();
    if (typed) { urlInput.value = ""; addLinks([typed]); }
    const urls = queue.filter((q) => q.type === "link").map((q) => q.url);
    const uploads = queue.filter((q) => q.type === "upload" && q.status === "ready").map((q) => q.id);
    if (!urls.length && !uploads.length) { syncSubmit(); return; }
    saveOptions();
    persistCookies();
    startJob(
      { urls, uploads, ...basePayload(), review: reviewToggle.checked },
      { onAccepted: () => clearQueue({ discardUploads: false }) },
    );
  };

  const submitCurrent = () => {
    if (queue.length) { submitBatch(); return; }
    const url = urlInput.value.trim();
    if (url) submitSingle(url, { review: reviewToggle.checked && mode() !== "original" });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCurrent();
  });

  const addTypedLink = () => {
    const value = urlInput.value.trim();
    if (!value) return;
    const urls = extractUrls(value);
    urlInput.value = "";
    if (!addLinks(urls.length ? urls : [value])) syncSubmit();
    syncUrlHint();
    urlInput.focus();
  };
  addLinkBtn.addEventListener("click", addTypedLink);
  urlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && queue.length) {
      event.preventDefault();
      addTypedLink();
    }
  });

  // Paste-to-go: a link pasted anywhere (except into another field) starts the job right away.
  // With a batch in progress, or several links at once, pasted links join the batch instead.
  const handlePaste = (event) => {
    const target = event.target;
    const isOtherField = target && target !== urlInput &&
      (target.tagName === "TEXTAREA" || target.tagName === "INPUT" || target.isContentEditable);
    if (isOtherField) return;
    const files = event.clipboardData ? [...event.clipboardData.files] : [];
    if (files.length && uploadsEnabled()) {
      event.preventDefault();
      addFiles(files);
      return;
    }
    const text = event.clipboardData ? event.clipboardData.getData("text") : "";
    const urls = extractUrls(text);
    if (!urls.length) return;
    event.preventDefault();
    if (queue.length || urls.length > 1 || jobActive()) {
      urlInput.value = "";
      if (!addLinks(urls)) syncSubmit();
      if (urls.length > 1) toast(`<b>${urls.length} links added</b> to the batch.`);
      syncUrlHint();
      return;
    }
    urlInput.value = urls[0];
    urlInput.focus();
    syncUrlHint();
    submitSingle(urls[0]);
  };
  document.addEventListener("paste", handlePaste);

  // Drag and drop files anywhere on the page.
  let dragDepth = 0;
  const hasFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes("Files");
  document.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    dragDepth += 1;
    dropzone.classList.add("over");
  });
  document.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropzone.classList.remove("over");
  });
  document.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  document.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove("over");
    addFiles(event.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  dropzone.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !fileInput.disabled) {
      event.preventDefault();
      fileInput.click();
    }
  });
  queueClear.addEventListener("click", () => clearQueue({ discardUploads: true }));

  const cancelJob = async (button) => {
    if (!currentJob) return;
    button.disabled = true;
    try { await fetch(`/api/jobs/${currentJob.id}`, { method: "DELETE" }); } finally { button.disabled = false; }
    clearTimeout(pollTimer);
    poll();
  };
  cancelBtn.addEventListener("click", () => cancelJob(cancelBtn));
  reviewCancelBtn.addEventListener("click", () => cancelJob(reviewCancelBtn));

  const onOptionsChanged = () => {
    syncModeUI();
    saveOptions();
    syncSubmit();
    if (currentJob && currentJob.status === "review") replan();
  };
  form.elements.mode.forEach((radio) => radio.addEventListener("change", onOptionsChanged));
  ["resolution", "fps", "engine", "std_canvas", "std_fps", "std_fit"].forEach((name) => {
    form.elements[name].addEventListener("change", onOptionsChanged);
  });
  reviewToggle.addEventListener("change", () => { saveOptions(); syncSubmit(); });
  urlInput.addEventListener("input", () => { syncUrlHint(); syncSubmit(); });
  cookiesInput.addEventListener("input", syncUrlHint);
  rememberCookies.addEventListener("change", persistCookies);

  // Leaving with uploads still waiting on the server: tidy them up.
  window.addEventListener("pagehide", () => {
    queue.filter((q) => q.type === "upload" && q.id).forEach((q) => {
      fetch(`/api/uploads/${q.id}`, { method: "DELETE", keepalive: true }).catch(() => {});
    });
  });

  // ------------------------------------------------------------------ boot

  loadOptions();
  syncModeUI();
  syncSubmit();
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
    syncSubmit();
  }
})();
