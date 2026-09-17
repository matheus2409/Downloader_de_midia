"use strict";

const MEDIA_ICONS = { video: "🎬", audio: "🎵", image: "🖼", document: "📄", other: "📦", pending: "⏳" };
const STATUS_LABELS = {
  queued: "Na fila",
  downloading: "Baixando",
  processando: "Processando",
  paused: "Pausado",
  agendado: "Agendado",
  reagendando: "Tentando de novo em breve",
  done: "Concluído",
  error: "Erro",
  cancelado: "Cancelado",
};
const TERMINAL = new Set(["done", "error", "cancelado"]);
const ACTIVE = new Set(["queued", "downloading", "processando", "agendado", "reagendando"]);
const WAITING = new Set(["agendado", "reagendando"]);

const state = {
  downloads: new Map(), // task_id -> row
  presets: {},
  currentPresetName: null,
  folderBrowsePath: null,
  batchNotified: true,
};

let cachedApiKey = "";

// ------------------------------------------------------------- elements
const el = (id) => document.getElementById(id);
const listEl = el("list");
const emptyHint = el("empty-hint");
const overallLabel = el("overall-label");
const overallFill = el("overall-fill");
const toastStack = el("toast-stack");

// ---------------------------------------------------------------- toast
function toast(message, timeout = 5000) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  toastStack.appendChild(node);
  setTimeout(() => node.remove(), timeout);
}

// ----------------------------------------------------------------- api
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (cachedApiKey) headers["X-API-Key"] = cachedApiKey;
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Erro ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

// --------------------------------------------------------- rendering
function ensureCard(taskId) {
  let card = document.getElementById(`card-${taskId}`);
  if (card) return card;

  card = document.createElement("div");
  card.className = "card";
  card.id = `card-${taskId}`;
  card.dataset.status = "queued";
  card.innerHTML = `
    <div class="card-top">
      <img class="card-thumb" style="display:none" />
      <span class="card-type">${MEDIA_ICONS.pending}</span>
      <span class="card-title" title="Abrir pasta"></span>
      <span class="card-status">${STATUS_LABELS.queued}</span>
      <button class="card-btn priority" title="Furar a fila">⤒</button>
      <button class="card-btn pause" title="Pausar">⏸</button>
      <button class="card-btn danger remove" title="Remover">✕</button>
    </div>
    <div class="card-progress"><div class="card-progress-fill"></div></div>
    <div class="card-meta"></div>
  `;

  card.querySelector(".priority").addEventListener("click", () => api(`/downloads/${taskId}/prioritize`, { method: "POST" }));
  card.querySelector(".pause").addEventListener("click", () => {
    const row = state.downloads.get(taskId);
    if (!row) return;
    if (row.status === "paused") {
      api(`/downloads/${taskId}/resume`, { method: "POST" });
    } else if (WAITING.has(row.status)) {
      api(`/downloads/${taskId}/start-now`, { method: "POST" });
    } else {
      api(`/downloads/${taskId}/pause`, { method: "POST" });
    }
  });
  card.querySelector(".remove").addEventListener("click", () => api(`/downloads/${taskId}/remove`, { method: "POST" }));
  card.querySelector(".card-title").addEventListener("click", () => {
    const row = state.downloads.get(taskId);
    const path = row && (row.path || row.folder);
    if (path) api("/open-folder", { method: "POST", body: JSON.stringify({ path }) }).catch((e) => toast(e.message));
  });

  listEl.appendChild(card);
  return card;
}

function formatScheduledTime(epochSeconds) {
  if (!epochSeconds) return "em breve";
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} às ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderCard(taskId) {
  const row = state.downloads.get(taskId);
  if (!row) return;
  const card = ensureCard(taskId);
  card.dataset.status = row.status;

  const title = card.querySelector(".card-title");
  title.textContent = row.url || taskId;

  card.querySelector(".card-status").textContent = STATUS_LABELS[row.status] || row.status;
  card.querySelector(".card-progress-fill").style.width = `${Math.round(row.percent || 0)}%`;

  const meta = card.querySelector(".card-meta");
  if (row.status === "error") meta.textContent = row.text || "Falhou";
  else if (row.status === "paused") meta.textContent = "Pausado — clique em ▶ para retomar";
  else if (row.status === "cancelado") meta.textContent = "Removido";
  else if (row.status === "done") meta.textContent = "Concluído";
  else if (row.status === "agendado") meta.textContent = `Agendado para ${formatScheduledTime(row.scheduled_for)} — clique em ▶ para iniciar agora`;
  else if (row.status === "reagendando") meta.textContent = `Erro parece passageiro — tentando de novo perto de ${formatScheduledTime(row.scheduled_for)}`;
  else meta.textContent = row.text || "";

  const typeIcon = card.querySelector(".card-type");
  typeIcon.textContent = MEDIA_ICONS[row.media_type] || MEDIA_ICONS.pending;

  const thumb = card.querySelector(".card-thumb");
  if (row.thumbnail) {
    thumb.src = row.thumbnail;
    thumb.style.display = "";
  }

  const pauseBtn = card.querySelector(".pause");
  const priorityBtn = card.querySelector(".priority");
  if (TERMINAL.has(row.status)) {
    pauseBtn.style.display = "none";
  } else {
    pauseBtn.style.display = "";
    if (row.status === "paused") {
      pauseBtn.textContent = "▶";
      pauseBtn.title = "Retomar";
    } else if (WAITING.has(row.status)) {
      pauseBtn.textContent = "▶";
      pauseBtn.title = "Iniciar agora";
    } else {
      pauseBtn.textContent = "⏸";
      pauseBtn.title = "Pausar";
    }
  }
  priorityBtn.style.display = row.status === "queued" ? "" : "none";

  applySearchFilterToCard(taskId);
  emptyHint.style.display = state.downloads.size ? "none" : "";
  refreshOverall();
}

function removeCard(taskId) {
  const card = document.getElementById(`card-${taskId}`);
  if (card) card.remove();
  state.downloads.delete(taskId);
  emptyHint.style.display = state.downloads.size ? "none" : "";
  refreshOverall();
}

function applySearchFilterToCard(taskId) {
  const card = document.getElementById(`card-${taskId}`);
  const row = state.downloads.get(taskId);
  if (!card || !row) return;
  const query = (el("search-input").value || "").toLowerCase().trim();
  if (!query) {
    card.style.display = "";
    return;
  }
  const haystack = `${row.url || ""} ${row.path || ""}`.toLowerCase();
  card.style.display = haystack.includes(query) ? "" : "none";
}

function refreshOverall() {
  const rows = [...state.downloads.values()];
  const total = rows.length;
  if (!total) {
    overallFill.style.width = "0%";
    overallLabel.textContent = "Nada na fila";
    state.batchNotified = true;
    return;
  }
  const done = rows.filter((r) => (r.percent || 0) >= 100).length;
  const avg = rows.reduce((sum, r) => sum + (r.percent || 0), 0) / total;
  overallFill.style.width = `${Math.round(avg)}%`;
  overallLabel.textContent = `${done}/${total} concluídos`;

  if (done === total && !state.batchNotified) {
    state.batchNotified = true;
    notifyBatchComplete(done);
  } else if (done < total) {
    state.batchNotified = false;
  }
}

function notifyBatchComplete(count) {
  toast(`${count} download(s) concluído(s).`);
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("Downloader de mídias", { body: `${count} download(s) concluído(s).` });
  }
}

// --------------------------------------------------------- websocket
function connectWebSocket() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (event) => handleEvent(JSON.parse(event.data));
  ws.onclose = () => setTimeout(connectWebSocket, 1500);
  ws.onerror = () => ws.close();
}

function handleEvent(data) {
  switch (data.type) {
    case "created":
      state.downloads.set(data.task_id, {
        url: data.url, folder: data.folder, status: data.status, percent: 0, text: "",
        scheduled_for: data.scheduled_for,
      });
      state.batchNotified = false;
      renderCard(data.task_id);
      break;
    case "status": {
      const row = state.downloads.get(data.task_id);
      if (row) {
        row.status = data.status;
        if ("scheduled_for" in data) row.scheduled_for = data.scheduled_for;
        renderCard(data.task_id);
      }
      break;
    }
    case "progress": {
      const row = state.downloads.get(data.task_id);
      if (row) {
        row.percent = data.percent;
        if (data.text) row.text = data.text;
        renderCard(data.task_id);
      }
      break;
    }
    case "thumbnail": {
      const row = state.downloads.get(data.task_id);
      if (row) {
        row.thumbnail = data.data_url;
        renderCard(data.task_id);
      }
      break;
    }
    case "file_ready": {
      const row = state.downloads.get(data.task_id);
      if (row) {
        row.path = data.path;
        const ext = (data.path.split(".").pop() || "").toLowerCase();
        row.media_type = classifyExt(ext);
        renderCard(data.task_id);
      }
      break;
    }
    case "finished": {
      const row = state.downloads.get(data.task_id);
      if (!row) break;
      if (data.success) {
        row.status = "done";
        row.percent = 100;
      } else if (data.message === "pausado") {
        row.status = "paused";
      } else if (data.message === "cancelado") {
        row.status = "cancelado";
        row.percent = 100;
      } else {
        row.status = "error";
        row.percent = 100;
        row.text = data.message;
      }
      renderCard(data.task_id);
      break;
    }
    case "removed":
      removeCard(data.task_id);
      break;
    case "toast":
      toast(data.message);
      break;
  }
}

const VIDEO_EXTS = new Set(["mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "wmv", "3gp"]);
const AUDIO_EXTS = new Set(["mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "wma"]);
const IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic"]);
const DOC_EXTS = new Set(["pdf", "doc", "docx", "txt", "csv", "xlsx", "pptx", "zip", "rar", "7z"]);
function classifyExt(ext) {
  if (VIDEO_EXTS.has(ext)) return "video";
  if (AUDIO_EXTS.has(ext)) return "audio";
  if (IMAGE_EXTS.has(ext)) return "image";
  if (DOC_EXTS.has(ext)) return "document";
  return "other";
}

// ----------------------------------------------------------- bootstrap
async function loadInitialState() {
  try {
    const settings = await api("/settings");
    cachedApiKey = settings.api_key || "";
    el("folder-input").value = settings.output_dir || "";
    el("concurrent-input").value = settings.max_concurrent || 3;
    state.presets = settings.presets || {};
    populatePresetSelect(settings.last_preset);
    el("ffmpeg-banner").classList.toggle("visible", !settings.ffmpeg_available);
    setupCookiesModal(settings);
    setupIntegrationModal(settings);
  } catch (e) {
    toast(`Não consegui carregar as configurações: ${e.message}`);
  }

  try {
    const { downloads } = await api("/downloads");
    for (const row of downloads) {
      state.downloads.set(row.task_id, row);
      renderCard(row.task_id);
    }
  } catch (e) {
    toast(`Não consegui carregar downloads em andamento: ${e.message}`);
  }

  try {
    const health = await api("/diagnostics/health");
    const bits = [];
    // limiar de 3 pra não incomodar por causa de uma falha isolada --
    // abaixo disso pode ser só um vídeo privado de verdade, não um
    // padrão que valha um aviso.
    const showCookiesBtn = health.login_required_count >= 3;
    if (showCookiesBtn) {
      bits.push(
        `${health.login_required_count} downloads recentes falharam porque o site pediu login (geralmente YouTube).`
      );
    }
    if (health.ytdlp && health.ytdlp.outdated) {
      bits.push(
        `yt-dlp desatualizado (${health.ytdlp.installed} instalado, ${health.ytdlp.latest} disponível) — rode "pip install -U yt-dlp".`
      );
    }
    if (health.gallery_dl && health.gallery_dl.outdated) {
      bits.push(
        `gallery-dl desatualizado (${health.gallery_dl.installed} instalado, ${health.gallery_dl.latest} disponível) — rode "pip install -U gallery-dl".`
      );
    }
    if (bits.length) {
      el("health-banner-text").textContent = bits.join(" ");
      el("health-banner-btn").style.display = showCookiesBtn ? "" : "none";
      el("health-banner").classList.add("visible");
    }
  } catch (e) {
    // best-effort -- checagem de "saúde" nunca deve atrapalhar o carregamento normal
  }
}

function populatePresetSelect(selected) {
  const select = el("preset-select");
  select.innerHTML = "";
  for (const name of Object.keys(state.presets)) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  if (selected && state.presets[selected]) select.value = selected;
}

// ------------------------------------------------------------- actions
async function startDownloads() {
  const text = el("url-box").value.trim();
  if (!text) return toast("Cole ao menos um link antes de baixar.");
  const urls = text.split("\n").map((u) => u.trim()).filter(Boolean);

  const folder = el("folder-input").value.trim();
  if (!folder) return toast("Escolha uma pasta de destino.");

  let scheduledFor = null;
  if (el("schedule-check").checked) {
    scheduledFor = el("schedule-datetime").value;
    if (!scheduledFor) return toast("Escolha a data/hora do agendamento, ou desmarque a opção.");
  }

  try {
    const disk = await api(`/disk-space?folder=${encodeURIComponent(folder)}`);
    if (disk.low) {
      const freeMb = Math.round(disk.free_bytes / (1024 * 1024));
      if (!confirm(`Sobram só ${freeMb} MB nessa pasta. Continuar mesmo assim?`)) return;
    }
  } catch (e) {
    // if the check itself fails, don't block the download over it
  }

  const preset = el("preset-select").value;
  const subtitles = el("subtitles-check").checked;
  toast(
    scheduledFor
      ? "Agendando... pastas e playlists grandes podem levar alguns segundos para expandir."
      : "Analisando link(s)... pastas e playlists grandes podem levar alguns segundos."
  );
  try {
    await api("/downloads", {
      method: "POST",
      body: JSON.stringify({ urls, folder, preset, subtitles, scheduled_for: scheduledFor }),
    });
    el("url-box").value = "";
  } catch (e) {
    toast(`Erro ao iniciar downloads: ${e.message}`);
  }
}

function appendUrls(lines) {
  const box = el("url-box");
  const current = box.value;
  const addition = lines.join("\n");
  box.value = current ? `${current}\n${addition}` : addition;
}

function readTextFile(file, onLines) {
  const reader = new FileReader();
  reader.onload = () => {
    const lines = String(reader.result).split("\n").map((l) => l.trim()).filter(Boolean);
    onLines(lines);
  };
  reader.readAsText(file);
}

// ------------------------------------------------------- drag and drop
const urlBox = el("url-box");
urlBox.addEventListener("dragover", (e) => {
  e.preventDefault();
  urlBox.classList.add("dragover");
});
urlBox.addEventListener("dragleave", () => urlBox.classList.remove("dragover"));
urlBox.addEventListener("drop", (e) => {
  e.preventDefault();
  urlBox.classList.remove("dragover");
  const files = [...(e.dataTransfer.files || [])];
  const txtFile = files.find((f) => f.name.toLowerCase().endsWith(".txt"));
  if (txtFile) {
    readTextFile(txtFile, (lines) => {
      appendUrls(lines);
      toast(`${lines.length} link(s) importados de ${txtFile.name}`);
    });
    return;
  }
  const text = e.dataTransfer.getData("text/plain");
  if (text) appendUrls([text.trim()]);
});

// ------------------------------------------------------------- wiring
el("download-btn").addEventListener("click", startDownloads);

el("import-btn").addEventListener("click", () => el("import-file-input").click());
el("import-file-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  readTextFile(file, (lines) => {
    appendUrls(lines);
    toast(`${lines.length} link(s) importados de ${file.name}`);
  });
  e.target.value = "";
});

el("concurrent-input").addEventListener("change", (e) => {
  const value = parseInt(e.target.value, 10) || 3;
  api("/settings", { method: "POST", body: JSON.stringify({ max_concurrent: value }) });
});

el("search-input").addEventListener("input", () => {
  for (const taskId of state.downloads.keys()) applySearchFilterToCard(taskId);
});

el("schedule-check").addEventListener("change", (e) => {
  el("schedule-datetime").style.display = e.target.checked ? "" : "none";
});

el("pause-all-btn").addEventListener("click", () => api("/downloads/pause_all", { method: "POST" }));
el("resume-all-btn").addEventListener("click", () => api("/downloads/resume_all", { method: "POST" }));
el("retry-failed-btn").addEventListener("click", async () => {
  const result = await api("/downloads/retry_failed", { method: "POST" });
  if (result.count) toast(`Tentando de novo ${result.count} download(s).`);
});
el("clear-done-btn").addEventListener("click", () => api("/downloads/clear_finished", { method: "POST" }));

// ---------------------------------------------------------------- modals
function openModal(id) {
  el(id).classList.add("visible");
}
function closeModal(id) {
  el(id).classList.remove("visible");
}
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});
document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeModal(backdrop.id);
  });
});

// --- history modal
let cachedHistoryEntries = [];
function renderHistoryList(filterQuery) {
  const listNode = el("history-list");
  const query = (filterQuery || "").toLowerCase().trim();
  const filtered = !query
    ? cachedHistoryEntries
    : cachedHistoryEntries.filter((e) => `${e.title} ${e.url}`.toLowerCase().includes(query));
  listNode.innerHTML = "";
  if (!filtered.length) {
    listNode.innerHTML = `<p class="hint">${cachedHistoryEntries.length ? "Nada encontrado." : "Nada baixado ainda."}</p>`;
    return;
  }
  for (const entry of filtered) {
    const item = document.createElement("div");
    item.className = "list-item";
    item.innerHTML = `<span>${MEDIA_ICONS[entry.media_type] || "📦"}</span><span class="lbl">${entry.title}</span><span class="hint">${entry.timestamp}</span>`;
    item.addEventListener("click", () => api("/open-folder", { method: "POST", body: JSON.stringify({ path: entry.path }) }).catch((e) => toast(e.message)));
    listNode.appendChild(item);
  }
}
el("history-btn").addEventListener("click", async () => {
  openModal("history-modal");
  el("history-search-input").value = "";
  const listNode = el("history-list");
  listNode.innerHTML = "Carregando...";
  try {
    const { entries } = await api("/history");
    cachedHistoryEntries = entries;
    renderHistoryList("");
  } catch (e) {
    listNode.innerHTML = `<p class="hint">Erro ao carregar: ${e.message}</p>`;
  }
});
el("history-search-input").addEventListener("input", (e) => renderHistoryList(e.target.value));
el("history-clear-btn").addEventListener("click", async () => {
  if (!confirm("Apagar todo o histórico?")) return;
  await api("/history", { method: "DELETE" });
  el("history-btn").click();
});

// --- folder browser modal
async function loadFolder(path) {
  const data = await api(`/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`);
  state.folderBrowsePath = data.path;
  el("folder-current-path").textContent = data.path;
  el("folder-up-btn").disabled = !data.parent;
  el("folder-up-btn").dataset.parent = data.parent || "";
  const listNode = el("folder-list");
  listNode.innerHTML = "";
  for (const dirName of data.dirs) {
    const item = document.createElement("div");
    item.className = "list-item";
    item.innerHTML = `<span>📁</span><span class="lbl">${dirName}</span>`;
    item.addEventListener("click", () => loadFolder(`${data.path}/${dirName}`));
    listNode.appendChild(item);
  }
}
el("browse-btn").addEventListener("click", () => {
  openModal("folder-modal");
  loadFolder(el("folder-input").value || null);
});
el("folder-up-btn").addEventListener("click", (e) => {
  const parent = e.target.dataset.parent;
  if (parent) loadFolder(parent);
});
el("folder-choose-btn").addEventListener("click", () => {
  el("folder-input").value = state.folderBrowsePath;
  closeModal("folder-modal");
});

// --- preset editor modal
function renderPresetList() {
  const listNode = el("preset-list");
  listNode.innerHTML = "";
  for (const name of Object.keys(state.presets)) {
    const item = document.createElement("div");
    item.className = "list-item";
    item.innerHTML = `<span class="lbl">${name}</span>`;
    item.addEventListener("click", () => selectPreset(name));
    listNode.appendChild(item);
  }
}
function selectPreset(name) {
  state.currentPresetName = name;
  el("preset-json-input").value = JSON.stringify(state.presets[name] || {}, null, 2);
}
el("preset-edit-btn").addEventListener("click", () => {
  openModal("preset-modal");
  renderPresetList();
  const first = Object.keys(state.presets)[0];
  if (first) selectPreset(first);
});
el("preset-new-btn").addEventListener("click", () => {
  const name = prompt("Nome do novo preset:");
  if (!name || state.presets[name]) return;
  state.presets[name] = { format: "bv*+ba/b" };
  renderPresetList();
  selectPreset(name);
});
el("preset-remove-btn").addEventListener("click", async () => {
  if (!state.currentPresetName) return;
  if (Object.keys(state.presets).length <= 1) return toast("Precisa sobrar pelo menos um preset.");
  if (!confirm(`Remover "${state.currentPresetName}"?`)) return;
  try {
    await api(`/presets/${encodeURIComponent(state.currentPresetName)}`, { method: "DELETE" });
    delete state.presets[state.currentPresetName];
    state.currentPresetName = null;
    el("preset-json-input").value = "";
    renderPresetList();
    populatePresetSelect();
  } catch (e) {
    toast(e.message);
  }
});
el("preset-save-btn").addEventListener("click", async () => {
  if (!state.currentPresetName) return;
  let options;
  try {
    options = JSON.parse(el("preset-json-input").value || "{}");
  } catch (e) {
    return toast("JSON inválido — confira a sintaxe.");
  }
  state.presets[state.currentPresetName] = options;
  await api("/presets", { method: "POST", body: JSON.stringify({ name: state.currentPresetName, options }) });
  populatePresetSelect(state.currentPresetName);
  toast("Preset salvo.");
});

// --- cookies/login modal
const COOKIE_BROWSER_LABELS = {
  chrome: "Chrome", chromium: "Chromium", firefox: "Firefox", edge: "Edge",
  brave: "Brave", opera: "Opera (não cobre Opera GX)", vivaldi: "Vivaldi",
  safari: "Safari", whale: "Whale",
};

function cookiesModeFromSettings(settings) {
  if (settings.cookies_file) return "file";
  if (settings.cookies_from_browser) return "browser";
  return "none";
}

function updateCookiesFieldsVisibility() {
  const mode = el("cookies-mode-select").value;
  el("cookies-file-field").style.display = mode === "file" ? "" : "none";
  el("cookies-browser-field").style.display = mode === "browser" ? "" : "none";
}

function setupCookiesModal(settings) {
  const browserSelect = el("cookies-browser-select");
  browserSelect.innerHTML = "";
  for (const value of settings.supported_cookie_browsers || []) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = COOKIE_BROWSER_LABELS[value] || value;
    browserSelect.appendChild(option);
  }

  el("cookies-mode-select").value = cookiesModeFromSettings(settings);
  el("cookies-file-input").value = settings.cookies_file || "";
  if (settings.cookies_from_browser) browserSelect.value = settings.cookies_from_browser;
  updateCookiesFieldsVisibility();
}

el("cookies-btn").addEventListener("click", () => openModal("cookies-modal"));
el("health-banner-btn").addEventListener("click", () => openModal("cookies-modal"));
el("cookies-mode-select").addEventListener("change", updateCookiesFieldsVisibility);
el("cookies-save-btn").addEventListener("click", async () => {
  const mode = el("cookies-mode-select").value;
  const payload = {
    cookies_file: mode === "file" ? el("cookies-file-input").value.trim() : "",
    cookies_from_browser: mode === "browser" ? el("cookies-browser-select").value : "",
  };
  try {
    await api("/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Configuração de login/cookies salva.");
    closeModal("cookies-modal");
  } catch (e) {
    toast(`Não consegui salvar: ${e.message}`);
  }
});

// --- diagnostics modal (achar downloads que vieram como thumbnail)
let diagnosticsItems = [];
el("diagnostics-btn").addEventListener("click", async () => {
  openModal("diagnostics-modal");
  const listNode = el("diagnostics-list");
  const summary = el("diagnostics-summary");
  summary.textContent = "Procurando...";
  listNode.innerHTML = "";
  try {
    const { items } = await api("/diagnostics/suspicious-downloads");
    diagnosticsItems = items;
    if (!items.length) {
      summary.textContent = "Nada suspeito encontrado no histórico atual — nenhum item parece ter vindo como thumbnail no lugar do conteúdo pedido.";
      return;
    }
    summary.textContent = `${items.length} item(ns) que provavelmente vieram como thumbnail em vez do conteúdo pedido. Marque os que quer re-baixar (o link original é usado de novo, com a pasta/qualidade atuais).`;
    items.forEach((item, i) => {
      const row = document.createElement("div");
      row.className = "list-item";
      row.style.cursor = "default";
      row.innerHTML = `
        <input type="checkbox" class="diag-check" data-index="${i}" checked />
        <span class="lbl">${item.title}</span>
        <span class="hint">${item.timestamp}</span>
      `;
      listNode.appendChild(row);
    });
  } catch (e) {
    summary.textContent = `Erro ao buscar: ${e.message}`;
  }
});
el("diagnostics-select-all-btn").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll(".diag-check")];
  const allChecked = boxes.every((b) => b.checked);
  boxes.forEach((b) => (b.checked = !allChecked));
});
el("diagnostics-redownload-btn").addEventListener("click", async () => {
  const selected = [...document.querySelectorAll(".diag-check:checked")].map(
    (b) => diagnosticsItems[Number(b.dataset.index)].url
  );
  if (!selected.length) return toast("Marque pelo menos um item.");
  const folder = el("folder-input").value.trim();
  if (!folder) return toast("Escolha uma pasta de destino (campo \"Salvar em\") antes de re-baixar.");
  const preset = el("preset-select").value;
  try {
    await api("/downloads", { method: "POST", body: JSON.stringify({ urls: selected, folder, preset }) });
    toast(`Re-baixando ${selected.length} item(ns).`);
    closeModal("diagnostics-modal");
  } catch (e) {
    toast(`Erro: ${e.message}`);
  }
});

// --- integration modal (chave de API + webhook)
function setupIntegrationModal(settings) {
  el("api-key-input").value = settings.api_key || "";
  el("webhook-url-input").value = settings.webhook_url || "";
}
el("integration-btn").addEventListener("click", () => openModal("integration-modal"));
el("integration-save-btn").addEventListener("click", async () => {
  const payload = {
    api_key: el("api-key-input").value.trim(),
    webhook_url: el("webhook-url-input").value.trim(),
  };
  try {
    await api("/settings", { method: "POST", body: JSON.stringify(payload) });
    cachedApiKey = payload.api_key;
    toast("Configuração de integração salva.");
    closeModal("integration-modal");
  } catch (e) {
    toast(`Não consegui salvar: ${e.message}`);
  }
});

// -------------------------------------------------------------- start
if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission();
}
connectWebSocket();
loadInitialState();
