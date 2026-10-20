const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const settingsKey = "lwrag-settings";
let libraryState = { documents: [], indexed_count: 0, indexed: false };
let latestAnswer = "";
let toastTimer;
let modelSystemProfile = null;
let selectedSetupModel = "";
let setupJob = { status: "idle" };
let setupPollTimer = null;
let lastNotifiedSetupJob = "";

function getSettings() {
  const defaults = { ollama_url: "http://localhost:11434", chat_model: "qwen2.5:7b", embedding_model: "nomic-embed-text" };
  try { return { ...defaults, ...JSON.parse(localStorage.getItem(settingsKey) || "{}") }; }
  catch (_) { return defaults; }
}

function applyTheme(theme) {
  const value = ["light", "dark", "system"].includes(theme) ? theme : "system";
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("lwrag-theme", value); } catch (_) {}
  $$('[data-theme-option]').forEach((option) => {
    const selected = option.dataset.themeOption === value;
    option.setAttribute("aria-checked", String(selected));
    option.tabIndex = selected ? 0 : -1;
  });
  updateThemeColor();
}

function applyAccent(accent) {
  const value = ["blue", "teal", "green", "indigo", "amber"].includes(accent) ? accent : "blue";
  const label = value[0].toUpperCase() + value.slice(1);
  document.documentElement.dataset.accent = value;
  try { localStorage.setItem("lwrag-accent", value); } catch (_) {}
  $("#accent-trigger").setAttribute("aria-label", `Accent color: ${label}`);
  $("#accent-trigger").title = `Choose accent color · ${label}`;
  $$('input[name="accent-color"]').forEach((input) => { input.checked = input.value === value; });
}

function updateThemeColor() {
  const theme = document.documentElement.dataset.theme;
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  $('meta[name="theme-color"]').content = dark ? "#111418" : "#f7f8fc";
}

function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function formatInline(value) {
  return value
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/\[(\d+)\]/g, '<button class="inline-citation" type="button" data-source="$1" aria-label="Open source $1">[$1]</button>');
}

function renderAnswerMarkdown(text) {
  const blocks = String(text).trim().split(/\n\s*\n/).filter(Boolean);
  return blocks.map((block) => {
    const lines = block.split("\n");
    if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
      return `<ul>${lines.map((line) => `<li>${formatInline(escapeHTML(line.replace(/^\s*[-*]\s+/, "")))}</li>`).join("")}</ul>`;
    }
    if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
      return `<ol>${lines.map((line) => `<li>${formatInline(escapeHTML(line.replace(/^\s*\d+[.)]\s+/, "")))}</li>`).join("")}</ol>`;
    }
    return `<p>${lines.map((line) => formatInline(escapeHTML(line))).join("<br>")}</p>`;
  }).join("");
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return "unknown";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatModelSize(bytes) {
  if (bytes === null || bytes === undefined) return "unknown";
  if (bytes === 0) return "0 B";
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${Math.round(bytes / 1_000_000)} MB`;
  return `${Math.round(bytes / 1_000)} KB`;
}

function toast(message, kind = "") {
  const region = $("#toast-region");
  region.replaceChildren();
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  region.append(item);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => item.remove(), 4600);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch (_) { data = {}; }
  if (!response.ok) {
    let detail = data.detail || data.message || `Request failed (${response.status}).`;
    if (typeof detail === "object") detail = [detail.message, ...(detail.problems || [])].filter(Boolean).join("\n");
    throw new Error(String(detail));
  }
  return data;
}

function renderLibrary(state) {
  libraryState = state;
  const list = $("#document-list");
  list.replaceChildren();
  $("#document-count").textContent = state.documents.length;
  $("#index-button").disabled = state.documents.length === 0;
  $("#send-button").disabled = !state.indexed || !$("#question-input").value.trim();
  $("#passage-count").textContent = state.indexed_count ? `${state.indexed_count.toLocaleString()} passages indexed` : "No passages indexed";
  $("#index-status").textContent = state.indexed ? "Ready to search" : (state.documents.length ? "Needs indexing" : "Not indexed");
  $("#index-status").classList.toggle("ready", state.indexed);
  $("#library-summary-title").textContent = state.documents.length ? `${state.documents.length} source${state.documents.length === 1 ? "" : "s"} in your library` : "A private research space";

  if (!state.documents.length) {
    const empty = document.createElement("div");
    empty.className = "library-empty";
    empty.innerHTML = '<span class="empty-icon" aria-hidden="true"><img src="/static/icons/description_24px.svg" alt="" /></span><span>Your library is empty</span><small>Add a PDF or DOCX to begin</small>';
    list.append(empty);
    return;
  }

  for (const item of state.documents) {
    const row = document.createElement("div");
    row.className = "document-row";
    const fileIcon = document.createElement("span");
    fileIcon.className = "document-type-icon";
    fileIcon.setAttribute("aria-hidden", "true");
    fileIcon.innerHTML = '<img src="/static/icons/description_24px.svg" alt="" />';
    const copy = document.createElement("div");
    copy.className = "document-copy";
    const name = document.createElement("div");
    name.className = "document-name";
    name.textContent = item.name;
    name.title = item.name;
    const meta = document.createElement("div");
    meta.className = "document-meta";
    meta.innerHTML = `<span>${escapeHTML(item.kind)}</span><span aria-hidden="true">·</span><span>${formatSize(item.size)}</span>${item.indexed ? '<span class="indexed">· Indexed</span>' : ""}`;
    copy.append(name, meta);
    const remove = document.createElement("button");
    remove.className = "icon-button remove-document";
    remove.type = "button";
    remove.title = `Remove ${item.name}`;
    remove.setAttribute("aria-label", `Remove ${item.name}`);
    remove.innerHTML = '<img src="/static/icons/delete_24px.svg" alt="" />';
    remove.addEventListener("click", () => removeDocument(item.name));
    row.append(fileIcon, copy, remove);
    list.append(row);
  }
}

async function refreshLibrary() {
  try { renderLibrary(await api("/api/library")); }
  catch (error) { toast(`Could not load your library: ${error.message}`, "error"); }
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const form = new FormData();
  files.forEach((file) => form.append("files", file, file.name));
  $("#dropzone").classList.add("uploading");
  try {
    const result = await api("/api/documents", { method: "POST", body: form });
    await refreshLibrary();
    if (result.saved.length) toast(`${result.saved.length} document${result.saved.length === 1 ? "" : "s"} added. Index the library to search them.`, "success");
    if (result.skipped.length) toast(result.skipped.join(" "), "error");
  } catch (error) { toast(error.message, "error"); }
  finally { $("#dropzone").classList.remove("uploading"); $("#file-input").value = ""; }
}

async function removeDocument(name) {
  if (!confirm(`Remove “${name}” from this library? The index will need to be rebuilt.`)) return;
  try {
    await api(`/api/documents/${encodeURIComponent(name)}`, { method: "DELETE" });
    $("#answer-area").hidden = true;
    $("#welcome").hidden = false;
    await refreshLibrary();
    toast("Document removed. Re-index the remaining library to search it.", "success");
  } catch (error) { toast(error.message, "error"); }
}

function modelSettings() {
  return {
    ollama_url: $("#ollama-url").value.trim().replace(/\/$/, ""),
    chat_model: $("#chat-model").value.trim(),
    embedding_model: $("#embedding-model").value.trim(),
  };
}

function showBusy(message) {
  $("#answer-area").hidden = false;
  $("#welcome").hidden = true;
  $("#answer-body").innerHTML = `<div class="busy-row"><span class="spinner" aria-hidden="true"></span><span>${escapeHTML(message)}</span></div>`;
  $("#sources-section").hidden = true;
}

async function indexLibrary() {
  if (!libraryState.documents.length) return;
  const button = $("#index-button");
  button.disabled = true;
  button.classList.add("is-loading");
  button.querySelector("span").textContent = "Indexing…";
  $("#index-status").textContent = "Indexing";
  try {
    const result = await api("/api/index", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(modelSettings()) });
    await refreshLibrary();
    $("#answer-area").hidden = true;
    $("#welcome").hidden = false;
    if (result.problems.length) toast(`Indexed ${result.indexed_count.toLocaleString()} passages. ${result.problems[0]}`, "success");
    else toast(`Library ready — ${result.indexed_count.toLocaleString()} passages indexed.`, "success");
  } catch (error) {
    await refreshLibrary();
    toast(error.message, "error");
  } finally {
    button.classList.remove("is-loading");
    button.querySelector("span").textContent = "Index library";
    button.disabled = libraryState.documents.length === 0;
  }
}

async function askQuestion(question) {
  const text = question.trim();
  if (!text) return;
  if (!libraryState.indexed) {
    toast("Add and index at least one document before asking a question.");
    return;
  }
  $("#question-echo").textContent = text;
  $("#question-input").value = "";
  resizeQuestionInput();
  $("#send-button").disabled = true;
  showBusy("Searching your library and preparing a cited answer…");
  try {
    const result = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...modelSettings(), question: text }),
    });
    latestAnswer = result.answer;
    $("#answer-body").innerHTML = renderAnswerMarkdown(result.answer);
    renderSources(result.sources);
    $("#answer-subtitle").textContent = `Grounded in ${result.sources.length} retrieved passage${result.sources.length === 1 ? "" : "s"}`;
    $("#answer-area").hidden = false;
    $("#welcome").hidden = true;
    requestAnimationFrame(() => $("#answer-area").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }));
  } catch (error) {
    $("#answer-body").innerHTML = `<p class="error-copy">${escapeHTML(error.message)}</p>`;
    $("#sources-section").hidden = true;
    toast(error.message, "error");
  } finally {
    $("#send-button").disabled = !libraryState.indexed || !$("#question-input").value.trim();
    $("#question-input").focus();
  }
}

function renderSources(sources) {
  const list = $("#source-list");
  list.replaceChildren();
  $("#source-count").textContent = `${sources.length} passage${sources.length === 1 ? "" : "s"}`;
  for (const source of sources) {
    const card = document.createElement("article");
    card.className = "source-card";
    card.id = `source-${source.number}`;
    const meta = document.createElement("div");
    meta.className = "source-meta";
    meta.innerHTML = `<span class="source-number">${source.number}</span><span class="source-title"></span><span aria-hidden="true">·</span><span></span>`;
    $(".source-title", meta).textContent = source.source;
    meta.lastElementChild.textContent = source.location;
    const excerpt = document.createElement("p");
    excerpt.className = "source-text";
    excerpt.textContent = source.text;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "source-open";
    toggle.textContent = "Show full passage";
    toggle.addEventListener("click", () => {
      const open = card.classList.toggle("expanded");
      toggle.textContent = open ? "Show less" : "Show full passage";
    });
    card.append(meta, excerpt, toggle);
    list.append(card);
  }
  $("#sources-section").hidden = false;
}

function resizeQuestionInput() {
  const field = $("#question-input");
  field.style.height = "auto";
  field.style.height = `${Math.min(field.scrollHeight, 190)}px`;
  $("#send-button").disabled = !libraryState.indexed || !field.value.trim();
}

function initializeSettings() {
  const settings = getSettings();
  $("#ollama-url").value = settings.ollama_url;
  $("#chat-model").value = settings.chat_model;
  $("#embedding-model").value = settings.embedding_model;
}

function selectedModelProfile() {
  return modelSystemProfile?.models.find((model) => model.id === $("#setup-model").value) || null;
}

function setSetupWarning(message = "", tone = "") {
  const warning = $("#setup-warning");
  warning.textContent = message;
  warning.dataset.tone = tone;
  warning.hidden = !message;
}

function renderSystemSummary(profile) {
  const system = profile.system;
  const memory = system.memory_bytes ? `${formatSize(system.memory_bytes)} RAM` : "RAM could not be detected";
  const platformName = [system.os, system.os_release, system.architecture].filter(Boolean).join(" · ");
  const cpu = [system.cpu, system.cpu_threads ? `${system.cpu_threads} CPU threads` : ""].filter(Boolean).join(" · ") || "Processor details unavailable";
  const gpu = system.gpu ? `${system.gpu.name}${system.gpu.vram_bytes ? ` · ${formatSize(system.gpu.vram_bytes)} VRAM` : ""}` : "No dedicated GPU detected; recommendations use system memory";
  $("#system-summary").innerHTML = `<strong>${escapeHTML(platformName)}</strong><span>${escapeHTML(memory)} · ${escapeHTML(cpu)}</span><span class="system-subline">${escapeHTML(gpu)}</span>`;
}

function renderSetupEstimate() {
  const estimate = $("#setup-estimate");
  const selected = selectedModelProfile();
  const consent = $("#setup-consent");
  if (!modelSystemProfile || !selected) {
    estimate.textContent = "Choose a model after the system check finishes.";
    $("#setup-consent-copy").textContent = "I reviewed the download size and want to install these models locally.";
    consent.disabled = true;
    return;
  }

  const fullModelStorage = selected.size_bytes + selected.embedding_size_bytes;
  const missingBytes = selected.download_bytes;
  const freeBytes = modelSystemProfile.storage.free_bytes;
  const lines = [
    `<strong>${escapeHTML(selected.id)} + ${escapeHTML(selected.embedding_model)}</strong>`,
    `<span>Model files after setup: about ${formatModelSize(fullModelStorage)} total. Download for this setup: ${formatModelSize(missingBytes)}.</span>`,
    `<span>LightWeightRAG and its Python environment currently use ${formatSize(modelSystemProfile.system.app_footprint_bytes)} on this computer; that space is already in use. Your books and index use additional space.</span>`,
    `<span>${escapeHTML(modelSystemProfile.storage.location_label)}: ${formatSize(freeBytes)} free. Keep at least ${formatModelSize(selected.required_free_bytes)} free for this download and temporary headroom.</span>`,
  ];
  estimate.innerHTML = lines.join("");
  $("#setup-consent-copy").textContent = `I reviewed the estimate and want to download about ${formatModelSize(missingBytes)} of model files locally.`;
  consent.disabled = !selected.ram_ok || !selected.storage_ok || !modelSystemProfile.ollama.running || !modelSystemProfile.ollama.local_url;
}

function updateSetupWarning() {
  const help = $("#ollama-help");
  help.hidden = true;
  if (!modelSystemProfile) {
    setSetupWarning("Checking this computer and Ollama…");
    return;
  }
  const { system, ollama, minimum_ram_bytes: minimumRam } = modelSystemProfile;
  const selected = selectedModelProfile();
  if (!system.memory_bytes) {
    setSetupWarning("We could not read this computer’s RAM, so we cannot safely recommend or download a model. Check the system report and try again.", "error");
    return;
  }
  if (system.memory_bytes < minimumRam) {
    setSetupWarning(`This computer has ${formatSize(system.memory_bytes)} RAM. The smallest model in this guide needs at least ${formatSize(minimumRam)} to run alongside the app. We will not start a download. RAM is the limiting factor; clearing disk space will not fix it.`, "error");
    return;
  }
  if (!ollama.local_url) {
    setSetupWarning("For privacy, the model downloader works only with Ollama running on this computer. Set the address to http://localhost:11434.", "error");
    return;
  }
  if (!ollama.running) {
    help.hidden = false;
    $("#ollama-help-copy").textContent = ollama.installed
      ? "Ollama is installed but not running. Open Ollama, wait for it to start, then refresh this check."
      : "Ollama is not installed or running. Install it from the official page, open it once, then refresh this check.";
    setSetupWarning(ollama.error || "Start Ollama to continue.");
    return;
  }
  if (!selected) {
    setSetupWarning("No model in this guide fits the detected RAM. We will not download a model that is unlikely to run reliably.", "error");
    return;
  }
  if (!selected.ram_ok) {
    setSetupWarning(`${selected.label} needs at least ${formatSize(selected.min_ram_bytes)} RAM. Choose a smaller model that fits this computer.`, "error");
    return;
  }
  if (modelSystemProfile.storage.free_bytes === null) {
    setSetupWarning("We could not check free space in the Ollama model folder, so downloads are disabled until storage can be measured.", "error");
    return;
  }
  if (!selected.storage_ok) {
    const smallerAvailable = modelSystemProfile.models.filter((model) => model.ram_ok && model.storage_ok && model.size_bytes < selected.size_bytes).at(-1);
    const shortfall = formatModelSize(selected.storage_shortfall_bytes);
    const ramSpace = system.memory_bytes >= selected.min_ram_bytes;
    const advice = ramSpace
      ? ` Your RAM already supports this model; freeing about ${shortfall} of disk space may be easier than upgrading memory.`
      : "";
    const fallback = smallerAvailable ? ` You can choose the smaller ${smallerAvailable.id} model now.` : " Free space or choose a smaller model.";
    setSetupWarning(`There is not enough free storage for ${selected.id} and download headroom. Free about ${shortfall} more.${advice}${fallback}`, "error");
    return;
  }
  const recommended = modelSystemProfile.models.find((model) => model.id === modelSystemProfile.recommended_model);
  if (recommended && selected.size_bytes < recommended.size_bytes && !recommended.storage_ok) {
    setSetupWarning(`Your RAM supports the stronger ${recommended.id}, but it needs about ${formatModelSize(recommended.storage_shortfall_bytes)} more free space. If possible, freeing disk space lets you use it; your RAM already qualifies. The selected ${selected.id} model fits now.`);
    return;
  }
  if (selected.id === modelSystemProfile.recommended_model && !selected.installed) {
    setSetupWarning(`This is the strongest model in the guide that fits the detected ${formatSize(system.memory_bytes)} RAM. Review the download and storage estimate before continuing.`);
    return;
  }
  setSetupWarning(selected.installed ? "Both selected models are already installed. Continue to select them for this workspace." : "This model fits the detected memory and available storage. Review the estimate before continuing.");
}

function updateSetupButton() {
  const button = $("#setup-button");
  const selected = selectedModelProfile();
  const busy = setupJob.status === "running";
  const canSetup = Boolean(
    modelSystemProfile && selected && selected.can_setup && $("#setup-consent").checked && !busy,
  );
  button.disabled = !canSetup;
  const label = button.querySelector("span");
  if (busy) label.textContent = "Downloading and setting up…";
  else if (setupJob.status === "error") label.textContent = "Retry model setup";
  else if (selected?.installed) label.textContent = "Use installed models";
  else label.textContent = "Download and set up";
}

function renderSystemProfile(profile) {
  modelSystemProfile = profile;
  renderSystemSummary(profile);
  const select = $("#setup-model");
  const previous = selectedSetupModel || select.value;
  select.replaceChildren();
  for (const model of profile.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.disabled = !model.ram_ok;
    option.textContent = `${model.label} · ${formatModelSize(model.size_bytes)}`;
    select.append(option);
  }
  const availablePrevious = profile.models.some((model) => model.id === previous && model.ram_ok);
  const preferred = availablePrevious ? previous : profile.recommended_model;
  if (preferred) select.value = preferred;
  else {
    const firstCompatible = profile.models.find((model) => model.ram_ok);
    if (firstCompatible) select.value = firstCompatible.id;
  }
  selectedSetupModel = select.value;
  select.disabled = !systemMemoryAvailable(profile);
  renderSetupEstimate();
  updateSetupWarning();
  updateSetupButton();
}

function systemMemoryAvailable(profile) {
  return Boolean(profile?.system?.memory_bytes);
}

async function refreshSystemProfile() {
  const url = $("#ollama-url").value.trim() || "http://localhost:11434";
  $("#system-summary").textContent = "Checking this computer…";
  try {
    const profile = await api(`/api/system?ollama_url=${encodeURIComponent(url)}`);
    renderSystemProfile(profile);
    return profile;
  } catch (error) {
    modelSystemProfile = null;
    $("#system-summary").textContent = "System check unavailable.";
    setSetupWarning(error.message, "error");
    updateSetupButton();
    throw error;
  }
}

function renderSetupJob(job) {
  setupJob = job || { status: "idle" };
  const progressPanel = $("#setup-progress");
  progressPanel.hidden = setupJob.status === "idle";
  if (progressPanel.hidden) {
    updateSetupButton();
    return;
  }
  const progress = Math.max(0, Math.min(100, Number(setupJob.progress) || 0));
  $("#setup-progress-label").textContent = setupJob.phase || setupJob.message || "Preparing local models…";
  $("#setup-progress-percent").textContent = `${progress}%`;
  $("#setup-progress-bar").value = progress;
  $("#setup-progress-detail").textContent = setupJob.message || "";
  progressPanel.dataset.state = setupJob.status;

  if (setupJob.status === "complete") {
    const settings = getSettings();
    const changed = settings.chat_model !== setupJob.chat_model || settings.embedding_model !== setupJob.embedding_model;
    if (changed) {
      settings.chat_model = setupJob.chat_model;
      settings.embedding_model = setupJob.embedding_model;
      localStorage.setItem(settingsKey, JSON.stringify(settings));
      initializeSettings();
    }
    if (setupJob.job_id && setupJob.job_id !== lastNotifiedSetupJob) {
      lastNotifiedSetupJob = setupJob.job_id;
      toast("Local models are downloaded and ready.", "success");
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("LightWeightRAG models are ready", { body: `${setupJob.chat_model} and ${setupJob.embedding_model} are set up locally.` });
      }
      $("#setup-consent").checked = false;
      refreshSystemProfile().catch(() => {});
    }
  } else if (setupJob.status === "error" && setupJob.job_id && setupJob.job_id !== lastNotifiedSetupJob) {
    lastNotifiedSetupJob = setupJob.job_id;
    toast(`Model setup needs attention: ${setupJob.error || setupJob.message}`, "error");
  }
  updateSetupButton();
}

async function pollSetupJob(jobId) {
  clearTimeout(setupPollTimer);
  try {
    const job = await api("/api/models/setup/status");
    renderSetupJob(job);
    if (job.status === "running" && job.job_id === jobId) {
      setupPollTimer = setTimeout(() => pollSetupJob(jobId), 1100);
    }
  } catch (error) {
    $("#setup-progress-detail").textContent = `Could not check setup progress: ${error.message}`;
    setupPollTimer = setTimeout(() => pollSetupJob(jobId), 2500);
  }
}

async function loadSetupJob() {
  try {
    const job = await api("/api/models/setup/status");
    renderSetupJob(job);
    if (job.status === "running") pollSetupJob(job.job_id);
  } catch (_) {}
}

async function startModelSetup() {
  const selected = selectedModelProfile();
  if (!selected || !selected.can_setup || !$("#setup-consent").checked) return;
  const button = $("#setup-button");
  button.disabled = true;
  try {
    const job = await api("/api/models/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ollama_url: $("#ollama-url").value.trim() || "http://localhost:11434", chat_model: selected.id }),
    });
    renderSetupJob(job);
    if (job.status === "running") pollSetupJob(job.job_id);
    else if (job.status === "complete") renderSetupJob(job);
  } catch (error) {
    toast(error.message, "error");
    setSetupWarning(error.message, "error");
    updateSetupButton();
  }
}

function installHandlers() {
  const picker = $("#file-input");
  const dropzone = $("#dropzone");
  picker.addEventListener("change", () => uploadFiles(picker.files));
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); picker.click(); }
  });
  ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));
  const themeOptions = $$('[data-theme-option]');
  themeOptions.forEach((option, index) => {
    option.addEventListener("click", () => applyTheme(option.dataset.themeOption));
    option.addEventListener("keydown", (event) => {
      let nextIndex;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % themeOptions.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + themeOptions.length) % themeOptions.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = themeOptions.length - 1;
      else return;
      event.preventDefault();
      applyTheme(themeOptions[nextIndex].dataset.themeOption);
      themeOptions[nextIndex].focus();
    });
  });

  const accentTrigger = $("#accent-trigger");
  const accentPopover = $("#accent-popover");
  const closeAccentPicker = (restoreFocus = false) => {
    accentPopover.hidden = true;
    accentTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) accentTrigger.focus();
  };
  accentTrigger.addEventListener("click", () => {
    const opening = accentPopover.hidden;
    accentPopover.hidden = !opening;
    accentTrigger.setAttribute("aria-expanded", String(opening));
    if (opening) $('input[name="accent-color"]:checked', accentPopover)?.focus();
  });
  $$('input[name="accent-color"]').forEach((input) => input.addEventListener("change", () => {
    applyAccent(input.value);
    closeAccentPicker(true);
  }));
  document.addEventListener("click", (event) => {
    if (!accentPopover.hidden && !event.target.closest(".accent-picker")) closeAccentPicker();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !accentPopover.hidden) closeAccentPicker(true);
  });
  $("#index-button").addEventListener("click", indexLibrary);
  $("#question-input").addEventListener("input", resizeQuestionInput);
  $("#question-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#question-form").requestSubmit(); }
  });
  $("#question-form").addEventListener("submit", (event) => { event.preventDefault(); askQuestion($("#question-input").value); });
  $$(".suggestion-card").forEach((button) => button.addEventListener("click", () => {
    $("#question-input").value = button.dataset.prompt;
    resizeQuestionInput();
    $("#question-input").focus();
  }));

  const dialog = $("#settings-dialog");
  $("#open-settings").addEventListener("click", () => {
    initializeSettings();
    dialog.showModal();
    refreshSystemProfile().catch(() => {});
    loadSetupJob();
  });
  $("#refresh-system").addEventListener("click", () => refreshSystemProfile().catch(() => {}));
  $("#ollama-url").addEventListener("change", () => refreshSystemProfile().catch(() => {}));
  $("#setup-model").addEventListener("change", () => {
    if (selectedSetupModel !== $("#setup-model").value) {
      $("#setup-consent").checked = false;
      if (setupJob.status !== "running") {
        setupJob = { status: "idle" };
        $("#setup-progress").hidden = true;
      }
    }
    selectedSetupModel = $("#setup-model").value;
    renderSetupEstimate();
    updateSetupWarning();
    updateSetupButton();
  });
  $("#setup-consent").addEventListener("change", updateSetupButton);
  $("#setup-button").addEventListener("click", startModelSetup);
  $("#cancel-settings").addEventListener("click", () => dialog.close());
  $("#save-settings").addEventListener("click", () => {
    const settings = modelSettings();
    if (!settings.ollama_url || !settings.chat_model || !settings.embedding_model) { toast("Enter an Ollama address and both model names.", "error"); return; }
    localStorage.setItem(settingsKey, JSON.stringify(settings));
    dialog.close();
    toast("Model settings saved in this browser.", "success");
    refreshSystemProfile().catch(() => {});
  });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  $("#copy-answer").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(latestAnswer); toast("Answer copied.", "success"); }
    catch (_) { toast("Clipboard access is unavailable in this browser.", "error"); }
  });
  $("#answer-body").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source]");
    if (!button) return;
    const card = $(`#source-${button.dataset.source}`);
    if (card) { card.classList.add("expanded"); $(".source-open", card).textContent = "Show less"; card.scrollIntoView({ behavior: "smooth", block: "center" }); }
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", updateThemeColor);
}

applyTheme(document.documentElement.dataset.theme || "system");
applyAccent(document.documentElement.dataset.accent || "blue");
initializeSettings();
installHandlers();
refreshLibrary();
