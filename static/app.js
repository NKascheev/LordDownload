"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    form: $("link-form"),
    url: $("url"),
    paste: $("paste-btn"),
    analyze: $("analyze-btn"),
    analyzeLabel: $("analyze-label"),
    error: $("error"),
    errorText: $("error-text"),
    result: $("result"),
    thumbnail: $("video-thumbnail"),
    thumbnailFallback: $("video-thumbnail-fallback"),
    title: $("video-title"),
    channel: $("video-channel"),
    channelAvatar: $("channel-avatar"),
    channelAvatarFallback: $("channel-avatar-fallback"),
    duration: $("video-duration"),
    qualitiesChip: $("video-qualities"),
    tabs: $("mode-tabs"),
    panel: $("mode-panel"),
    fieldVideo: $("field-video"),
    fieldAudio: $("field-audio"),
    fieldSubtitles: $("field-subtitles"),
    quality: $("quality"),
    subtitle: $("subtitle"),
    subtitleHint: $("subtitle-hint"),
    download: $("download-btn"),
    downloadLabel: $("download-label"),
    fileNote: $("file-note"),
    progressPanel: $("progress-panel"),
    progressPhase: $("progress-phase"),
    progressPercent: $("progress-percent"),
    progressBar: $("progress-bar"),
    progressDetail: $("progress-detail"),
    progressSpeed: $("progress-speed"),
    readyPanel: $("ready-panel"),
    readyName: $("ready-name"),
    saveLink: $("save-link"),
  };

  const state = {
    csrf: null,
    sessionPromise: null,
    info: null,
    mode: "video",
    analyzing: false,
    downloading: false,
    jobId: null,
    jobTimer: null,
    pollFailures: 0,
    analyzeController: null,
    requestSequence: 0,
    retainedJobs: [],
    lastCompletedUrl: null,
  };

  const modeMeta = {
    video: {
      label: "Скачать видео",
      empty: "MP4",
    },
    audio: {
      label: "Скачать MP3",
      empty: "MP3 · 192 кбит/с",
    },
    subtitles: {
      label: "Скачать TXT",
      empty: "TXT · обычный текст",
    },
  };

  let languageNames = null;
  try {
    languageNames = new Intl.DisplayNames(["ru"], { type: "language" });
  } catch {
    languageNames = null;
  }

  function init() {
    bindEvents();
    els.analyze.disabled = true;
    ensureSession()
      .then(() => {
        clearError();
      })
      .catch((error) => {
        showError(humanError(error), false);
      })
      .finally(() => {
        updateControls();
      });
  }

  function bindEvents() {
    els.form.addEventListener("submit", handleAnalyze);
    els.paste.addEventListener("click", pasteFromClipboard);
    els.url.addEventListener("input", handleUrlInput);
    els.download.addEventListener("click", startDownload);
    els.quality.addEventListener("change", updateModeSummary);
    els.subtitle.addEventListener("change", updateModeSummary);
    els.tabs.addEventListener("click", handleTabClick);
    els.tabs.addEventListener("keydown", handleTabKeydown);
    window.addEventListener("pagehide", (event) => {
      if (!event.persisted) {
        discardOnExit();
      }
      stopPolling();
      if (state.analyzeController) {
        state.analyzeController.abort();
      }
    });
  }

  async function ensureSession() {
    if (state.csrf) {
      return;
    }
    if (!state.sessionPromise) {
      state.sessionPromise = (async () => {
        let response;
        try {
          response = await fetch("/api/bootstrap", {
            method: "GET",
            headers: { Accept: "application/json" },
            cache: "no-store",
            credentials: "same-origin",
          });
        } catch (cause) {
          throw new Error("Локальный сервер не отвечает. Убедитесь, что приложение запущено.");
        }
        let data = null;
        try {
          data = await response.json();
        } catch {
          data = null;
        }
        if (!response.ok || !data || typeof data.csrf_token !== "string") {
          throw new Error("Не удалось инициализировать интерфейс. Обновите страницу.");
        }
        state.csrf = data.csrf_token;
      })();
    }

    try {
      await state.sessionPromise;
    } finally {
      state.sessionPromise = null;
    }
  }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { Accept: "application/json" };
    const init = {
      method,
      headers,
      cache: "no-store",
      credentials: "same-origin",
      signal: options.signal,
    };

    if (options.body !== undefined) {
      await ensureSession();
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = state.csrf;
      init.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw error;
      }
      const networkError = new Error(
        "Нет связи с локальным сервером. Проверьте, что приложение запущено."
      );
      networkError.code = "network";
      throw networkError;
    }

    let data = null;
    const contentType = response.headers.get("Content-Type") || "";
    if (contentType.includes("application/json")) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }

    if (!response.ok) {
      const message =
        (data && data.error && data.error.message) ||
        `Сервер вернул ошибку ${response.status}.`;
      const error = new Error(message);
      error.code = (data && data.error && data.error.code) || "http_error";
      error.status = response.status;
      throw error;
    }

    return data;
  }

  function humanError(error) {
    if (!error) {
      return "Неизвестная ошибка. Попробуйте ещё раз.";
    }
    if (error.name === "AbortError") {
      return "";
    }
    if (error.code === "csrf") {
      return "Сессия устарела. Обновите страницу и повторите действие.";
    }
    if (error.message) {
      return error.message;
    }
    return "Не удалось выполнить действие. Попробуйте ещё раз.";
  }

  function handleUrlInput() {
    if (state.analyzing || state.downloading) {
      return;
    }
    if (state.info && els.url.value.trim() !== state.info.url) {
      resetResult();
    }
    clearError();
  }

  async function pasteFromClipboard() {
    if (!navigator.clipboard || typeof navigator.clipboard.readText !== "function") {
      els.url.focus();
      showError("Браузер не даёт доступ к буферу обмена. Вставьте ссылку вручную: Ctrl+V.");
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      if (!text || !text.trim()) {
        els.url.focus();
        return;
      }
      els.url.value = text.trim();
      els.url.dispatchEvent(new Event("input", { bubbles: true }));
      els.url.focus();
      clearError();
    } catch {
      els.url.focus();
      showError("Не удалось прочитать буфер обмена. Вставьте ссылку вручную: Ctrl+V.");
    }
  }

  async function handleAnalyze(event) {
    event.preventDefault();
    if (state.analyzing || state.downloading) {
      return;
    }

    const rawUrl = els.url.value.trim();
    if (!rawUrl) {
      showError("Вставьте ссылку на видео YouTube.");
      els.url.focus();
      return;
    }
    if (!looksLikeYouTubeUrl(rawUrl)) {
      showError("Похоже, это не ссылка на YouTube. Поддерживаются youtube.com и youtu.be.");
      els.url.focus();
      return;
    }

    clearError();
    resetResult();
    state.analyzing = true;
    state.requestSequence += 1;
    const sequence = state.requestSequence;
    state.analyzeController = new AbortController();
    updateControls();

    try {
      await ensureSession();
      const info = await api("/api/info", {
        method: "POST",
        body: { url: rawUrl },
        signal: state.analyzeController.signal,
      });
      if (sequence !== state.requestSequence) {
        return;
      }
      state.info = info;
      els.url.value = info.url;
      renderInfo(info);
      els.result.hidden = false;
      els.result.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "nearest",
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      if (sequence === state.requestSequence) {
        resetResult();
        showError(humanError(error));
      }
    } finally {
      if (sequence === state.requestSequence) {
        state.analyzing = false;
        state.analyzeController = null;
        updateControls();
      }
    }
  }

  function looksLikeYouTubeUrl(value) {
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      return false;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return false;
    }
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    return [
      "youtube.com",
      "www.youtube.com",
      "m.youtube.com",
      "music.youtube.com",
      "youtube-nocookie.com",
      "www.youtube-nocookie.com",
      "youtu.be",
      "www.youtu.be",
    ].includes(host);
  }

  function renderInfo(info) {
    els.title.textContent = info.title || "Видео YouTube";
    els.channel.textContent = info.channel || "YouTube";
    renderThumbnail(info);
    renderChannelAvatar(info);

    if (Number.isFinite(info.duration) && info.duration > 0) {
      const duration = formatDuration(info.duration);
      els.duration.textContent = duration;
      els.duration.hidden = false;
      els.duration.title = "Длительность";
      els.duration.setAttribute("aria-label", `Длительность: ${duration}`);
    } else {
      els.duration.hidden = true;
      els.duration.removeAttribute("aria-label");
    }

    const qualityCount = info.qualities.length;
    els.qualitiesChip.textContent = `${qualityCount} ${plural(qualityCount, "качество", "качества", "качеств")}`;
    els.qualitiesChip.hidden = qualityCount === 0;

    fillQualityOptions(info.qualities);
    fillSubtitleOptions(info.subtitles);
    updateSubtitleHint(info.subtitles);

    const modes = availableModes();
    if (!modes.video && !modes.audio && !modes.subtitles) {
      state.mode = "video";
      els.result.hidden = false;
      updateControls();
      showError("Для этого видео не найдены доступные форматы.");
      return;
    }

    state.mode = modes.video ? "video" : modes.audio ? "audio" : "subtitles";
    selectMode(state.mode, false);
    updateControls();
  }

  function renderThumbnail(info) {
    els.thumbnail.onload = () => {
      els.thumbnail.hidden = false;
      els.thumbnailFallback.hidden = true;
    };
    els.thumbnail.onerror = () => {
      els.thumbnail.hidden = true;
      els.thumbnailFallback.hidden = false;
    };
    els.thumbnailFallback.hidden = false;
    if (info.thumbnail_url) {
      els.thumbnail.alt = `Превью: ${info.title || "видео"}`;
      els.thumbnail.hidden = true;
      els.thumbnail.src = info.thumbnail_url;
    } else {
      els.thumbnail.removeAttribute("src");
      els.thumbnail.alt = "";
      els.thumbnail.hidden = true;
    }
  }

  function clearThumbnail() {
    els.thumbnail.onload = null;
    els.thumbnail.onerror = null;
    els.thumbnail.removeAttribute("src");
    els.thumbnail.alt = "";
    els.thumbnail.hidden = true;
    els.thumbnailFallback.hidden = false;
  }

  function renderChannelAvatar(info) {
    els.channelAvatar.onload = () => {
      els.channelAvatar.hidden = false;
      els.channelAvatarFallback.hidden = true;
    };
    els.channelAvatar.onerror = () => {
      els.channelAvatar.hidden = true;
      els.channelAvatarFallback.hidden = false;
    };
    els.channelAvatarFallback.hidden = false;
    if (info.channel_avatar_url) {
      els.channelAvatar.alt = info.channel
        ? `Логотип канала ${info.channel}`
        : "Логотип канала";
      els.channelAvatar.hidden = true;
      els.channelAvatar.src = info.channel_avatar_url;
    } else {
      els.channelAvatar.removeAttribute("src");
      els.channelAvatar.alt = "";
      els.channelAvatar.hidden = true;
    }
  }

  function clearChannelAvatar() {
    els.channelAvatar.onload = null;
    els.channelAvatar.onerror = null;
    els.channelAvatar.removeAttribute("src");
    els.channelAvatar.alt = "";
    els.channelAvatar.hidden = true;
    els.channelAvatarFallback.hidden = false;
  }

  function fillQualityOptions(qualities) {
    els.quality.replaceChildren();
    for (const quality of qualities) {
      const option = document.createElement("option");
      option.value = String(quality.height);
      option.textContent = qualityOptionLabel(quality);
      els.quality.append(option);
    }

    if (qualities.length === 0) {
      return;
    }
    const preferred =
      qualities.find((item) => item.height === 1080) ||
      qualities[0];
    els.quality.value = String(preferred.height);
  }

  function qualityOptionLabel(quality) {
    const parts = [];
    if (quality.height >= 2160) {
      parts.push("4K");
    } else if (quality.height >= 1440) {
      parts.push("2K");
    } else if (quality.height >= 1080) {
      parts.push("Full HD");
    } else if (quality.height >= 720) {
      parts.push("HD");
    }
    parts.push(`${quality.height}p`);
    if (quality.fps && quality.fps >= 50) {
      parts.push(`${quality.fps} fps`);
    }
    if (quality.hdr) {
      parts.push("HDR");
    }
    if (quality.codec) {
      parts.push(quality.codec);
    }
    let label = parts.join(" · ");
    if (quality.filesize) {
      label += ` · ~${formatBytes(quality.filesize)}`;
    }
    return label;
  }

  function fillSubtitleOptions(subtitles) {
    els.subtitle.replaceChildren();
    const manual = subtitles.filter((item) => !item.auto);
    const automatic = subtitles.filter((item) => item.auto);

    if (manual.length > 0) {
      const group = document.createElement("optgroup");
      group.label = "Оригинальные субтитры";
      for (const subtitle of manual) {
        group.append(subtitleOption(subtitle));
      }
      els.subtitle.append(group);
    }

    if (automatic.length > 0) {
      const group = document.createElement("optgroup");
      group.label = "Автоматические субтитры";
      for (const subtitle of automatic) {
        group.append(subtitleOption(subtitle));
      }
      els.subtitle.append(group);
    }

    if (subtitles.length === 0) {
      return;
    }

    const preferred =
      subtitles.find((item) => !item.auto && item.code === "ru") ||
      subtitles.find((item) => !item.auto && item.code === "en") ||
      subtitles.find((item) => item.auto && item.code === "ru") ||
      subtitles.find((item) => item.auto && item.code === "en") ||
      subtitles[0];
    els.subtitle.value = preferred.code;
  }

  function subtitleOption(subtitle) {
    const option = document.createElement("option");
    option.value = subtitle.code;
    option.dataset.auto = subtitle.auto ? "true" : "false";
    option.textContent = `${languageLabel(subtitle.code)} · ${subtitle.code}`;
    return option;
  }

  function updateSubtitleHint(subtitles) {
    const automaticCount = subtitles.filter((item) => item.auto).length;
    if (subtitles.length === 0) {
      els.subtitleHint.textContent = "У этого видео нет доступных субтитров.";
      return;
    }
    if (automaticCount > 0) {
      els.subtitleHint.textContent =
        "Таймкоды и разметка будут убраны. Автоматические субтитры могут содержать ошибки распознавания.";
    } else {
      els.subtitleHint.textContent = "Таймкоды и разметка будут убраны — останется читаемый текст.";
    }
  }

  function languageLabel(code) {
    const fallback = code;
    if (!languageNames) {
      return fallback;
    }
    let languageCode = code;
    if (languageCode.endsWith("-orig")) {
      languageCode = languageCode.slice(0, -5);
    }
    let name;
    try {
      name = languageNames.of(languageCode);
    } catch {
      name = null;
    }
    if (!name || name.toLowerCase() === languageCode.toLowerCase()) {
      try {
        const base = languageCode.split("-")[0];
        name = languageNames.of(base);
      } catch {
        name = null;
      }
    }
    if (!name) {
      return fallback;
    }
    return name.charAt(0).toLocaleUpperCase("ru-RU") + name.slice(1);
  }

  function availableModes() {
    return {
      video: Boolean(state.info && state.info.qualities.length > 0),
      audio: Boolean(state.info && state.info.has_audio),
      subtitles: Boolean(state.info && state.info.subtitles.length > 0),
    };
  }

  function handleTabClick(event) {
    const tab = event.target.closest("[data-mode]");
    if (!tab || tab.disabled || state.analyzing || state.downloading) {
      return;
    }
    selectMode(tab.dataset.mode, false);
  }

  function handleTabKeydown(event) {
    const tabs = Array.from(els.tabs.querySelectorAll("[data-mode]:not(:disabled)"));
    if (tabs.length === 0) {
      return;
    }
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) {
      return;
    }
    let nextIndex = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    }
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    const next = tabs[nextIndex];
    selectMode(next.dataset.mode, true);
  }

  function selectMode(mode, focus) {
    const modes = availableModes();
    if (!modes[mode]) {
      return;
    }
    state.mode = mode;
    const tabs = els.tabs.querySelectorAll("[data-mode]");
    for (const tab of tabs) {
      const active = tab.dataset.mode === mode;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    }
    els.fieldVideo.hidden = mode !== "video";
    els.fieldAudio.hidden = mode !== "audio";
    els.fieldSubtitles.hidden = mode !== "subtitles";
    els.panel.setAttribute("aria-labelledby", `tab-${mode}`);
    if (focus) {
      const activeTab = els.tabs.querySelector(`[data-mode="${mode}"]`);
      if (activeTab) {
        activeTab.focus();
      }
    }
    updateModeSummary();
    updateControls();
  }

  function updateModeSummary() {
    const modes = availableModes();
    if (!state.info || !modes[state.mode]) {
      els.fileNote.textContent = "";
      els.downloadLabel.textContent = modeMeta[state.mode].label;
      return;
    }

    if (state.mode === "video") {
      const selected = getSelectedQuality();
      if (!selected) {
        els.fileNote.textContent = modeMeta.video.empty;
      } else {
        const size = selected.filesize ? ` · ~${formatBytes(selected.filesize)}` : "";
        els.fileNote.textContent = `MP4 · ${selected.height}p${size}`;
      }
    } else if (state.mode === "audio") {
      els.fileNote.textContent = modeMeta.audio.empty;
    } else {
      const option = els.subtitle.selectedOptions[0];
      const code = option ? option.value : "";
      els.fileNote.textContent = code
        ? `TXT · ${languageLabel(code)}`
        : modeMeta.subtitles.empty;
    }

    els.downloadLabel.textContent = state.downloading
      ? "Скачиваю…"
      : modeMeta[state.mode].label;
  }

  function getSelectedQuality() {
    if (!state.info) {
      return null;
    }
    const height = Number.parseInt(els.quality.value, 10);
    return state.info.qualities.find((item) => item.height === height) || null;
  }

  function updateControls() {
    const busy = state.analyzing || state.downloading;
    const modes = availableModes();

    els.url.disabled = busy;
    els.paste.disabled = state.analyzing;
    els.analyze.disabled = busy;
    els.analyze.classList.toggle("is-loading", state.analyzing);
    els.analyze.setAttribute("aria-busy", state.analyzing ? "true" : "false");
    els.analyzeLabel.textContent = state.analyzing ? "Ищу видео…" : "Получить варианты";

    const tabs = els.tabs.querySelectorAll("[data-mode]");
    for (const tab of tabs) {
      tab.disabled = busy || !modes[tab.dataset.mode];
    }

    const hasInfo = Boolean(state.info) && Boolean(modes[state.mode]);
    els.quality.disabled = busy || !modes.video;
    els.subtitle.disabled = busy || !modes.subtitles;
    els.download.disabled = busy || !hasInfo;
    els.download.classList.toggle("is-loading", state.downloading);
    if (!state.downloading) {
      els.downloadLabel.textContent = modeMeta[state.mode].label;
    }
    els.result.setAttribute("aria-busy", busy ? "true" : "false");
  }

  async function startDownload() {
    if (state.analyzing || state.downloading || !state.info) {
      return;
    }
    const modes = availableModes();
    if (!modes[state.mode]) {
      return;
    }

    const body = {
      url: state.info.url,
      mode: state.mode,
    };

    if (state.mode === "video") {
      const quality = getSelectedQuality();
      if (!quality) {
        showError("Выберите качество видео.");
        return;
      }
      body.height = quality.height;
    } else if (state.mode === "subtitles") {
      const option = els.subtitle.selectedOptions[0];
      if (!option) {
        showError("Выберите язык субтитров.");
        return;
      }
      body.subtitle_lang = option.value;
      body.auto_subtitle = option.dataset.auto === "true";
    }

    if (state.retainedJobs.length > 0 && state.info.url !== state.lastCompletedUrl) {
      void discardRetainedJobs();
    }

    stopPolling();
    state.jobId = null;
    clearError();
    hideReady();
    resetProgress();
    els.progressPanel.hidden = false;
    state.downloading = true;
    updateModeSummary();
    updateControls();

    try {
      const response = await api("/api/download", {
        method: "POST",
        body,
      });
      if (!response || typeof response.job_id !== "string") {
        throw new Error("Сервер не создал задачу скачивания.");
      }
      state.jobId = response.job_id;
      state.pollFailures = 0;
      schedulePoll();
    } catch (error) {
      state.downloading = false;
      els.progressPanel.hidden = true;
      updateControls();
      showError(humanError(error));
    }
  }

  function schedulePoll() {
    window.clearTimeout(state.jobTimer);
    state.jobTimer = window.setTimeout(pollJob, 850);
  }

  async function pollJob() {
    const jobId = state.jobId;
    if (!jobId) {
      return;
    }
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (state.jobId !== jobId) {
        return;
      }
      state.pollFailures = 0;
      if (job.status === "ready") {
        finishReady(job);
        return;
      }
      if (job.status === "error") {
        const message =
          (job.error && job.error.message) || "Не удалось скачать файл.";
        finishError(message);
        return;
      }
      updateProgress(job);
      schedulePoll();
    } catch (error) {
      if (state.jobId !== jobId) {
        return;
      }
      state.pollFailures += 1;
      if (state.pollFailures <= 3 && (!error || error.status !== 404)) {
        schedulePoll();
        return;
      }
      finishError(humanError(error) || "Не удалось получить состояние загрузки.");
    }
  }

  function updateProgress(job) {
    els.progressPanel.hidden = false;
    const percent = clampPercent(job.progress);
    els.progressBar.value = percent;
    els.progressBar.setAttribute("aria-valuenow", String(Math.round(percent)));
    els.progressPercent.textContent = percent > 0 ? `${Math.round(percent)}%` : "…";
    els.progressPhase.textContent = job.phase || "Скачивание…";

    if (Number.isFinite(job.downloaded_bytes) && job.downloaded_bytes > 0) {
      if (Number.isFinite(job.total_bytes) && job.total_bytes > 0) {
        els.progressDetail.textContent =
          `${formatBytes(job.downloaded_bytes)} из ${formatBytes(job.total_bytes)}`;
      } else {
        els.progressDetail.textContent = formatBytes(job.downloaded_bytes);
      }
    } else {
      els.progressDetail.textContent = "";
    }

    const speedParts = [];
    if (Number.isFinite(job.speed) && job.speed > 0) {
      speedParts.push(`${formatBytes(job.speed)}/с`);
    }
    if (Number.isFinite(job.eta) && job.eta > 0) {
      speedParts.push(`осталось ~${formatDuration(job.eta)}`);
    }
    els.progressSpeed.textContent = speedParts.join(" · ");
  }

  function finishReady(job) {
    if (!job.file || !job.file.url) {
      finishError("Сервер не вернул сведения о готовом файле.");
      return;
    }
    const completedJobId = state.jobId;
    const completedUrl = state.info ? state.info.url : null;
    state.jobId = null;
    state.downloading = false;
    stopPolling();
    els.progressPanel.hidden = true;
    els.progressBar.value = 100;
    els.progressPercent.textContent = "100%";
    const file = job.file;
    els.readyName.textContent =
      `${file.name}${file.size ? ` · ${formatBytes(file.size)}` : ""}`;
    els.readyName.title = file.name || "";
    els.saveLink.href = file.url;
    els.saveLink.download = file.name || "";
    els.readyPanel.hidden = false;
    if (completedJobId && completedUrl) {
      state.retainedJobs = state.retainedJobs.filter((item) => item.id !== completedJobId);
      state.retainedJobs.push({ id: completedJobId, url: completedUrl });
      if (state.retainedJobs.length > 20) {
        state.retainedJobs.shift();
      }
      state.lastCompletedUrl = completedUrl;
    }
    updateControls();
    updateModeSummary();
    els.saveLink.focus({ preventScroll: true });
  }

  function finishError(message) {
    state.jobId = null;
    state.downloading = false;
    stopPolling();
    els.progressPanel.hidden = true;
    updateControls();
    updateModeSummary();
    showError(message || "Не удалось скачать файл.");
  }

  function resetProgress() {
    els.progressBar.value = 0;
    els.progressBar.setAttribute("aria-valuenow", "0");
    els.progressPercent.textContent = "0%";
    els.progressPhase.textContent = "Подготовка…";
    els.progressDetail.textContent = "";
    els.progressSpeed.textContent = "";
  }

  function hideReady() {
    els.readyPanel.hidden = true;
    els.readyName.textContent = "";
    els.readyName.removeAttribute("title");
    els.saveLink.removeAttribute("href");
    els.saveLink.removeAttribute("download");
  }

  async function discardRetainedJobs() {
    const jobs = state.retainedJobs;
    state.retainedJobs = [];
    await Promise.all(
      jobs.map(async (job) => {
        try {
          await api("/api/jobs/delete", {
            method: "POST",
            body: { job_id: job.id },
          });
        } catch {
          // Если удалить не удалось, файл всё равно очистится по TTL.
        }
      })
    );
  }

  function discardOnExit() {
    if (!state.csrf) {
      return;
    }
    const ids = new Set(state.retainedJobs.map((job) => job.id));
    if (state.jobId) {
      ids.add(state.jobId);
    }
    for (const jobId of ids) {
      fetch("/api/jobs/delete", {
        method: "POST",
        keepalive: true,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": state.csrf,
        },
        body: JSON.stringify({ job_id: jobId }),
      }).catch(() => {});
    }
  }

  function resetResult() {
    stopPolling();
    state.info = null;
    state.mode = "video";
    state.jobId = null;
    els.result.hidden = true;
    els.quality.replaceChildren();
    els.subtitle.replaceChildren();
    els.quality.disabled = true;
    els.subtitle.disabled = true;
    els.qualitiesChip.hidden = true;
    els.duration.hidden = true;
    els.duration.removeAttribute("aria-label");
    els.title.textContent = "";
    els.channel.textContent = "";
    clearThumbnail();
    clearChannelAvatar();
    hideReady();
    els.progressPanel.hidden = true;
    resetProgress();
    updateControls();
    updateModeSummary();
  }

  function stopPolling() {
    window.clearTimeout(state.jobTimer);
    state.jobTimer = null;
  }

  function showError(message, scroll = true) {
    if (!message) {
      return;
    }
    els.errorText.textContent = message;
    els.error.hidden = false;
    if (scroll) {
      els.error.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "nearest",
      });
    }
  }

  function clearError() {
    els.error.hidden = true;
    els.errorText.textContent = "";
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 Б";
    }
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    const maximumFractionDigits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits }).format(value)} ${units[index]}`;
  }

  function formatDuration(totalSeconds) {
    if (!Number.isFinite(totalSeconds) || totalSeconds < 0) {
      return "";
    }
    const seconds = Math.round(totalSeconds);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
    }
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function plural(count, one, few, many) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) {
      return one;
    }
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
      return few;
    }
    return many;
  }

  function clampPercent(value) {
    if (!Number.isFinite(value)) {
      return 0;
    }
    return Math.min(100, Math.max(0, value));
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
