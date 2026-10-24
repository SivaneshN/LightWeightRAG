const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const settingsKey = "lwrag-settings";
const projectPreferencesKey = "lwrag-project-preferences";
const projectViewKey = "lwrag-project-view";
const multilingualEmbeddingModel = "nomic-embed-text-v2-moe";
const maxDocumentBytes = 100 * 1024 * 1024;
let libraryState = { documents: [], indexed_count: 0, indexed: false };
let projectsState = [];
let activeProjectId = null;
let projectViewMode = (() => {
  try { return localStorage.getItem(projectViewKey) === "list" ? "list" : "grid"; }
  catch (_) { return "grid"; }
})();
let indexJob = { status: "idle", progress: 0 };
let latestAnswer = "";
let toastTimer;
let modelSystemProfile = null;
let selectedSetupModel = "";
let setupJob = { status: "idle" };
let setupPollTimer = null;
let indexPollTimer = null;
let lastNotifiedIndexJob = "";
let lastNotifiedSetupJob = "";
let pendingDeleteModel = null;

function getSettings() {
  const defaults = { ollama_url: "http://localhost:11434", chat_model: "qwen3:4b", embedding_model: multilingualEmbeddingModel, model_directory: "" };
  try {
    const saved = JSON.parse(localStorage.getItem(settingsKey) || "{}");
    const settings = { ...defaults, ...saved };
    if (["qwen2.5:7b", "qwen2.5:7b:latest"].includes(settings.chat_model)) {
      settings.chat_model = defaults.chat_model;
      localStorage.setItem(settingsKey, JSON.stringify(settings));
    }
    if (settings.embedding_model === "nomic-embed-text") {
      settings.embedding_model = multilingualEmbeddingModel;
      localStorage.setItem(settingsKey, JSON.stringify(settings));
    }
    return settings;
  }
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

function libraryNeedsReindex(state = libraryState) {
  if (!state?.indexed) return false;
  const selectedEmbedding = normalizeModelName(getSettings().embedding_model);
  return !state.embedding_model || normalizeModelName(state.embedding_model) !== selectedEmbedding;
}

function getProjectPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(projectPreferencesKey) || localStorage.getItem("lwrag-topic-preferences") || "{}");
    return { activeProjectId: saved.activeProjectId || saved.activeTopicId };
  }
  catch (_) { return {}; }
}

function saveProjectPreferences() {
  try { localStorage.setItem(projectPreferencesKey, JSON.stringify({ activeProjectId })); } catch (_) {}
}

function contextProjects() {
  const active = projectsState.find((project) => project.id === activeProjectId);
  if (!active) return [];
  const contextIds = new Set([active.id, ...(active.included_project_ids || [])]);
  return projectsState.filter((project) => contextIds.has(project.id));
}

function contextProjectsWithDocuments() {
  return contextProjects().filter((project) => project.document_count > 0);
}

function selectedProjectReadiness() {
  const selected = contextProjectsWithDocuments();
  const model = normalizeModelName(getSettings().embedding_model);
  const unindexed = selected.filter((project) => !project.indexed);
  const mismatched = selected.filter((project) => project.indexed && normalizeModelName(project.embedding_model) !== model);
  return {
    selected,
    unindexed,
    mismatched,
    ready: selected.length > 0 && unindexed.length === 0 && mismatched.length === 0,
  };
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
  const needsReindex = libraryNeedsReindex(state);
  const modelsAvailable = configuredModelsAreInstalled(modelSystemProfile);
  const list = $("#document-list");
  list.replaceChildren();
  $("#document-count").textContent = state.documents.length;
  const activeProject = projectsState.find((project) => project.id === activeProjectId);
  const activeName = activeProject?.name || state.project_name || "General";
  $("#documents-title").textContent = `${activeName} documents`;
  $("#passage-count").textContent = needsReindex
    ? `${state.indexed_count.toLocaleString()} passages need re-indexing`
    : state.indexed_count ? `${state.indexed_count.toLocaleString()} passages indexed in ${activeName}` : "No passages indexed in this project";
  const activeReady = state.indexed && !needsReindex && modelsAvailable;
  $("#index-status").textContent = indexJob.status === "running" ? `Indexing · ${indexJob.progress || 0}%` : needsReindex ? "Re-index required" : !modelsAvailable ? "Model setup needed" : state.indexed ? "Indexed" : (state.documents.length ? "Needs indexing" : "Empty project");
  $("#index-status").classList.toggle("ready", activeReady && indexJob.status !== "running");
  $("#index-status").classList.toggle("needs-reindex", needsReindex && indexJob.status !== "running");
  $("#index-status").classList.toggle("model-missing", !modelsAvailable && !needsReindex && indexJob.status !== "running");
  $("#library-summary-title").textContent = state.documents.length ? `${state.documents.length} source${state.documents.length === 1 ? "" : "s"} in ${activeName}` : `Project · ${activeName}`;

  if (!state.documents.length) {
    const empty = document.createElement("div");
    empty.className = "library-empty";
    empty.innerHTML = '<span class="empty-icon" aria-hidden="true"><img src="/static/icons/description_24px.svg" alt="" /></span><span>This project has no files yet</span><small>Add a PDF or DOCX to begin</small>';
    list.append(empty);
    updateIndexControls();
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
    remove.disabled = indexJob.status === "running";
    remove.addEventListener("click", () => removeDocument(item.name));
    row.append(fileIcon, copy, remove);
    list.append(row);
  }
  updateIndexControls();
}

function projectStatus(project) {
  const stale = project.indexed && normalizeModelName(project.embedding_model) !== normalizeModelName(getSettings().embedding_model);
  if (!project.document_count) return "No documents yet";
  if (stale) return `${project.document_count} document${project.document_count === 1 ? "" : "s"} · Re-index needed`;
  if (project.indexed) return `${project.document_count} document${project.document_count === 1 ? "" : "s"} · Indexed`;
  return `${project.document_count} document${project.document_count === 1 ? "" : "s"} · Needs indexing`;
}

function updateWorkspaceMode() {
  const picker = !activeProjectId;
  const project = projectsState.find((item) => item.id === activeProjectId);
  $("#top").classList.toggle("project-picker", picker);
  $("#project-home").hidden = !picker;
  $("#browse-projects-sidebar").hidden = picker;
  $("#project-workspace-heading").hidden = picker;
  $("#active-project-title").textContent = project?.name || "";
  $("#workspace-name-value").textContent = project?.name || "Research projects";
  $("#query-dock").hidden = picker;
  if (picker) {
    $("#welcome").hidden = true;
    $("#answer-area").hidden = true;
  }
}

function renderProjectHome() {
  const grid = $("#project-home-grid");
  if (!grid) return;
  grid.classList.toggle("view-list", projectViewMode === "list");
  $("#project-view-grid").setAttribute("aria-pressed", String(projectViewMode === "grid"));
  $("#project-view-list").setAttribute("aria-pressed", String(projectViewMode === "list"));
  if (!activeProjectId) $("#document-count").textContent = projectsState.length;
  grid.replaceChildren();
  if (!projectsState.length) {
    const empty = document.createElement("p");
    empty.className = "project-home-empty";
    empty.textContent = "Create your first project to keep its research documents and questions together.";
    grid.append(empty);
    return;
  }
  for (const project of projectsState) {
    const item = document.createElement("div");
    item.className = "project-home-item";
    item.setAttribute("role", "listitem");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "project-home-card";
    open.setAttribute("aria-label", `Open ${project.name} project`);
    open.innerHTML = '<span class="project-home-icon" aria-hidden="true"><img src="/static/icons/folder_open_24px.svg" alt="" /></span><span class="project-home-copy"><strong></strong><small></small></span><img class="project-home-arrow" src="/static/icons/arrow_forward_24px.svg" alt="" />';
    $(".project-home-copy strong", open).textContent = project.name;
    $(".project-home-copy small", open).textContent = projectStatus(project);
    open.addEventListener("click", () => openProject(project.id));
    item.append(open);
    grid.append(item);
  }
}

function setProjectView(mode) {
  if (!["grid", "list"].includes(mode)) return;
  projectViewMode = mode;
  try { localStorage.setItem(projectViewKey, mode); } catch (_) {}
  renderProjectHome();
}

function updateIndexControls(renderProjects = true) {
  const busy = indexJob.status === "running";
  const readiness = selectedProjectReadiness();
  const canIndex = contextProjectsWithDocuments().length > 0;
  $("#index-button").disabled = !canIndex || busy;
  $("#index-button").classList.toggle("is-loading", busy);
  $("#index-button").querySelector("span").textContent = busy ? "Indexing…" : readiness.mismatched.length ? "Re-index project sources" : "Index project sources";
  $("#file-input").disabled = busy;
  $("#dropzone").classList.toggle("indexing", busy);
  $("#dropzone").setAttribute("aria-disabled", String(busy));
  $$(".remove-document").forEach((button) => { button.disabled = busy; });
  $("#add-project").disabled = busy;
  $("#send-button").disabled = busy || !readiness.ready || !configuredModelsAreInstalled(modelSystemProfile) || !$("#question-input").value.trim();
  if (renderProjects) renderProjectList();
}

async function refreshLibrary() {
  if (!activeProjectId) return;
  try { renderLibrary(await api(`/api/library?project_id=${encodeURIComponent(activeProjectId)}`)); }
  catch (error) { toast(`Could not load your library: ${error.message}`, "error"); }
}

async function refreshProjects() {
  try {
    const result = await api("/api/projects");
    projectsState = result.projects || [];
    const ids = new Set(projectsState.map((project) => project.id));
    const saved = getProjectPreferences();
    activeProjectId = ids.has(activeProjectId) ? activeProjectId : null;
    if (!activeProjectId && ids.has(saved.activeProjectId)) activeProjectId = saved.activeProjectId;
    saveProjectPreferences();
    renderProjectList();
    renderProjectHome();
    renderProjectContext();
    updateWorkspaceMode();
    if (activeProjectId) await refreshLibrary();
  } catch (error) {
    $("#project-list").innerHTML = '<p class="project-list-empty">Projects could not be loaded.</p>';
    toast(`Could not load projects: ${error.message}`, "error");
  }
}

function renderProjectList() {
  const list = $("#project-list");
  if (!list) return;
  list.replaceChildren();
  if (!projectsState.length) {
    const empty = document.createElement("p");
    empty.className = "project-list-empty";
    empty.textContent = "Create a project for your research topic.";
    list.append(empty);
    return;
  }
  const busy = indexJob.status === "running";
  for (const project of projectsState) {
    const row = document.createElement("div");
    row.className = `project-row${project.id === activeProjectId ? " active" : ""}`;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "project-open";
    open.disabled = busy;
    open.setAttribute("aria-label", `Open ${project.name} project`);
    open.setAttribute("aria-current", project.id === activeProjectId ? "page" : "false");
    open.innerHTML = '<img src="/static/icons/folder_open_24px.svg" alt="" /><span class="project-open-copy"><strong></strong><small></small></span><span class="project-current"></span>';
    $(".project-open-copy strong", open).textContent = project.name;
    $(".project-open-copy small", open).textContent = projectStatus(project).replace("No documents yet", "Empty · add documents");
    $(".project-current", open).textContent = project.id === activeProjectId ? "OPEN" : "";
    open.addEventListener("click", () => openProject(project.id));
    row.append(open);
    list.append(row);
  }
}

function renderProjectInclusions() {
  const list = $("#project-inclusions");
  if (!list) return;
  list.replaceChildren();
  if (!projectsState.length) {
    const empty = document.createElement("p");
    empty.className = "project-inclusions-empty";
    empty.textContent = "There are no other projects to include yet.";
    list.append(empty);
    return;
  }
  for (const project of projectsState) {
    const label = document.createElement("label");
    label.className = "include-project-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "included-project";
    checkbox.value = project.id;
    checkbox.setAttribute("aria-label", `Include documents from ${project.name}`);
    const copy = document.createElement("span");
    copy.className = "include-project-copy";
    const name = document.createElement("strong");
    name.textContent = project.name;
    const detail = document.createElement("small");
    detail.textContent = project.document_count
      ? `${project.document_count} document${project.document_count === 1 ? "" : "s"} · ${project.indexed ? "indexed" : "not indexed yet"}`
      : "No documents yet";
    copy.append(name, detail);
    label.append(checkbox, copy);
    list.append(label);
  }
}

async function openProject(projectId) {
  if (!projectsState.some((project) => project.id === projectId)) return;
  activeProjectId = projectId;
  saveProjectPreferences();
  $("#answer-area").hidden = true;
  $("#welcome").hidden = false;
  updateWorkspaceMode();
  renderProjectList();
  renderProjectContext();
  await refreshLibrary();
}

function showProjectPicker() {
  activeProjectId = null;
  saveProjectPreferences();
  renderProjectList();
  renderProjectHome();
  updateWorkspaceMode();
}

function renderProjectContext() {
  const project = projectsState.find((item) => item.id === activeProjectId);
  $("#project-context-name").textContent = project?.name || "General";
  $("#project-instructions").value = project?.instructions || "";
  $("#project-instructions-status").textContent = "";
}

async function saveProjectInstructions() {
  const project = projectsState.find((item) => item.id === activeProjectId);
  if (!project) return;
  const button = $("#save-project-instructions");
  const status = $("#project-instructions-status");
  button.disabled = true;
  status.textContent = "Saving…";
  try {
    const result = await api(`/api/projects/${encodeURIComponent(activeProjectId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instructions: $("#project-instructions").value }),
    });
    projectsState = projectsState.map((item) => item.id === activeProjectId ? result.project : item);
    status.textContent = "Saved";
    toast(`Instructions saved for ${project.name}.`, "success");
  } catch (error) {
    status.textContent = "Could not save";
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function createResearchProject(name, instructions, includedProjectIds) {
  const error = $("#project-form-error");
  error.hidden = true;
  const button = $("#save-project");
  button.disabled = true;
  try {
    const result = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, instructions, included_project_ids: includedProjectIds }),
    });
    await refreshProjects();
    await openProject(result.project.id);
    $("#create-project-dialog").close();
    $("#create-project-form").reset();
    toast(`${result.project.name} project created. Add documents to begin.`, "success");
  } catch (requestError) {
    error.textContent = requestError.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function uploadFiles(fileList) {
  if (indexJob.status === "running") {
    toast("Wait for indexing to finish before adding more documents.", "error");
    return;
  }
  const submittedFiles = [...fileList];
  const oversized = submittedFiles.filter((file) => file.size > maxDocumentBytes);
  const warning = $("#upload-warning");
  if (oversized.length) {
    const names = oversized.slice(0, 2).map((file) => file.name).join(", ");
    warning.textContent = `${oversized.length} file${oversized.length === 1 ? "" : "s"} skipped: ${names}${oversized.length > 2 ? ", and others" : ""}. Each file must be 100 MB or smaller.`;
    warning.hidden = false;
  } else {
    warning.hidden = true;
    warning.textContent = "";
  }
  const files = submittedFiles.filter((file) => file.size <= maxDocumentBytes);
  if (!files.length) {
    $("#file-input").value = "";
    return;
  }
  const form = new FormData();
  files.forEach((file) => form.append("files", file, file.name));
  $("#dropzone").classList.add("uploading");
  try {
    const result = await api(`/api/documents?project_id=${encodeURIComponent(activeProjectId)}`, { method: "POST", body: form });
    await refreshProjects();
    if (result.saved.length) toast(`${result.saved.length} document${result.saved.length === 1 ? "" : "s"} added to ${libraryState.project_name || "this project"}. Index this project's sources to search them.`, "success");
    if (result.skipped.length) toast(result.skipped.join(" "), "error");
  } catch (error) { toast(error.message, "error"); }
  finally { $("#dropzone").classList.remove("uploading"); $("#file-input").value = ""; }
}

async function removeDocument(name) {
  if (indexJob.status === "running") {
    toast("Wait for indexing to finish before removing documents.", "error");
    return;
  }
  if (!confirm(`Remove “${name}” from this library? The index will need to be rebuilt.`)) return;
  try {
    await api(`/api/documents/${encodeURIComponent(name)}?project_id=${encodeURIComponent(activeProjectId)}`, { method: "DELETE" });
    $("#answer-area").hidden = true;
    $("#welcome").hidden = false;
    await refreshProjects();
    toast("Document removed. Re-index this project's sources to update search.", "success");
  } catch (error) { toast(error.message, "error"); }
}

function modelSettings() {
  return {
    ollama_url: $("#ollama-url").value.trim().replace(/\/$/, ""),
    chat_model: $("#chat-model").value.trim(),
    embedding_model: $("#embedding-model").value.trim(),
    model_directory: $("#model-directory").value.trim(),
  };
}

function showBusy(message) {
  $("#answer-area").hidden = false;
  $("#welcome").hidden = true;
  $("#answer-body").innerHTML = `<div class="busy-row"><span class="spinner" aria-hidden="true"></span><span>${escapeHTML(message)}</span></div>`;
  $("#sources-section").hidden = true;
}

async function indexLibrary() {
  if (!activeProjectId || !contextProjectsWithDocuments().length || indexJob.status === "running") return;
  try {
    const job = await api("/api/index", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...modelSettings(), project_id: activeProjectId }) });
    $("#answer-area").hidden = true;
    $("#welcome").hidden = false;
    renderIndexJob(job);
    if (job.status === "running") pollIndexJob(job.job_id);
  } catch (error) {
    toast(error.message, "error");
  }
}

function formatDuration(seconds) {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 60) return `about ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `about ${hours} hr ${remainingMinutes} min` : `about ${hours} hr`;
}

function renderIndexJob(job, notify = true) {
  indexJob = job || { status: "idle", progress: 0 };
  const panel = $("#index-progress");
  panel.hidden = indexJob.status === "idle";
  updateIndexControls();
  if (panel.hidden) return;

  const progress = Math.max(0, Math.min(100, Number(indexJob.progress) || 0));
  $("#index-progress-label").textContent = indexJob.phase || "Preparing your library";
  $("#index-progress-percent").textContent = `${progress}%`;
  $("#index-progress-bar").value = progress;
  $("#index-progress-bar").setAttribute("aria-valuetext", `${progress} percent${indexJob.message ? ` · ${indexJob.message}` : ""}`);
  panel.dataset.state = indexJob.status;
  const parts = [indexJob.message || "Indexing your documents…"];
  if (indexJob.status === "running" && Number.isFinite(indexJob.eta_seconds) && indexJob.eta_seconds > 0) {
    parts.push(`Estimated remaining: ${formatDuration(indexJob.eta_seconds)} based on recent progress.`);
  } else if (indexJob.status === "running" && indexJob.phase === "Building multilingual search index" && Number(indexJob.completed_batches || 0) < Number(indexJob.total_batches || 0)) {
    parts.push("Calculating a time estimate from model speed…");
  }
  const detail = parts.join(" ");
  if ($("#index-progress-detail").textContent !== detail) $("#index-progress-detail").textContent = detail;

  const problems = $("#index-progress-problems");
  problems.replaceChildren();
  const items = indexJob.problems || [];
  for (const problem of items.slice(0, 4)) {
    const item = document.createElement("li");
    item.textContent = problem;
    problems.append(item);
  }
  if (items.length > 4) {
    const more = document.createElement("li");
    more.textContent = `And ${items.length - 4} more document issues.`;
    problems.append(more);
  }
  problems.hidden = !problems.children.length;

  if (indexJob.status === "running") {
    $("#index-status").textContent = `Indexing · ${progress}%`;
    $("#index-status").classList.remove("ready", "needs-reindex", "model-missing");
  } else if (indexJob.status === "error") {
    $("#index-status").textContent = "Indexing needs attention";
    $("#index-status").classList.remove("ready", "needs-reindex");
    $("#index-status").classList.add("model-missing");
    if (notify && indexJob.job_id && indexJob.job_id !== lastNotifiedIndexJob) {
      lastNotifiedIndexJob = indexJob.job_id;
      toast(indexJob.error || indexJob.message || "Indexing did not finish.", "error");
    }
  } else if (indexJob.status === "complete" && notify && indexJob.job_id && indexJob.job_id !== lastNotifiedIndexJob) {
    lastNotifiedIndexJob = indexJob.job_id;
    if (items.length) toast(`${indexJob.message} See the progress details for document issues.`, "success");
    else toast(indexJob.message || "Library indexing is complete.", "success");
    refreshProjects();
  }
}

async function pollIndexJob(jobId) {
  clearTimeout(indexPollTimer);
  try {
    const job = await api("/api/index/status");
    renderIndexJob(job);
    if (job.status === "running" && job.job_id === jobId) {
      indexPollTimer = setTimeout(() => pollIndexJob(jobId), 900);
    }
  } catch (error) {
    $("#index-progress-detail").textContent = `Could not check indexing progress: ${error.message}`;
    indexPollTimer = setTimeout(() => pollIndexJob(jobId), 2200);
  }
}

async function loadIndexJob() {
  try {
    const job = await api("/api/index/status");
    renderIndexJob(job, false);
    if (job.status === "running") pollIndexJob(job.job_id);
  } catch (_) {}
}

async function askQuestion(question) {
  const text = question.trim();
  if (!text) return;
  const readiness = selectedProjectReadiness();
  if (!readiness.ready) {
    const names = [...readiness.unindexed, ...readiness.mismatched].map((project) => project.name);
    toast(readiness.selected.length ? `Index this project's sources${names.length ? `: ${names.join(", ")}` : ""} before asking a question.` : "Open a project that contains documents before asking a question.");
    return;
  }
  if (indexJob.status === "running") {
    toast("Wait for indexing to finish before asking a question.");
    return;
  }
  if (!configuredModelsAreInstalled(modelSystemProfile)) {
    toast("Open Model settings to download or select the models for this library.");
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
      body: JSON.stringify({ ...modelSettings(), question: text, project_id: activeProjectId }),
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
    $("#send-button").disabled = indexJob.status === "running" || !selectedProjectReadiness().ready || !configuredModelsAreInstalled(modelSystemProfile) || !$("#question-input").value.trim();
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
    meta.innerHTML = `<span class="source-number">${source.number}</span><span class="source-title"></span><span class="source-project"></span><span aria-hidden="true">·</span><span class="source-location"></span>`;
    $(".source-title", meta).textContent = source.source;
    $(".source-project", meta).textContent = source.project || "General";
    $(".source-location", meta).textContent = source.location;
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
  $("#send-button").disabled = indexJob.status === "running" || !selectedProjectReadiness().ready || !configuredModelsAreInstalled(modelSystemProfile) || !field.value.trim();
}

function initializeSettings() {
  const settings = getSettings();
  $("#ollama-url").value = settings.ollama_url;
  $("#chat-model").value = settings.chat_model;
  $("#embedding-model").value = settings.embedding_model;
  $("#model-directory").value = settings.model_directory || modelSystemProfile?.storage?.model_directory || "";
}

function selectedModelProfile() {
  return modelSystemProfile?.models.find((model) => model.id === $("#setup-model").value) || null;
}

function normalizeModelName(name) {
  const value = String(name || "").trim().toLowerCase();
  return value.endsWith(":latest") ? value.slice(0, -7) : value;
}

function configuredModelsAreInstalled(profile) {
  const installed = new Set((profile?.ollama?.models || []).map((model) => normalizeModelName(model.name)));
  const settings = getSettings();
  return Boolean(
    profile?.ollama?.running &&
    installed.has(normalizeModelName(settings.chat_model)) &&
    installed.has(normalizeModelName(settings.embedding_model))
  );
}

function ollamaWillBeInstalled(profile = modelSystemProfile) {
  return Boolean(profile && !profile.ollama.running && !profile.ollama.installed);
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

function renderInstalledModels(profile) {
  const container = $("#installed-model-list");
  const models = profile?.ollama?.models || [];
  $("#installed-model-count").textContent = `${models.length} downloaded`;
  container.replaceChildren();
  if (!models.length) {
    const empty = document.createElement("p");
    empty.className = "model-list-empty";
    empty.textContent = profile?.ollama?.running ? "No models have been downloaded yet." : "Start Ollama to manage downloaded models.";
    container.append(empty);
    return;
  }
  for (const model of models) {
    const row = document.createElement("div");
    row.className = "installed-model-row";
    row.setAttribute("role", "listitem");
    const info = document.createElement("div");
    info.className = "installed-model-info";
    const name = document.createElement("strong");
    name.textContent = model.name;
    const size = document.createElement("span");
    size.textContent = formatSize(model.size_bytes);
    info.append(name, size);
    const remove = document.createElement("button");
    remove.className = "icon-button remove-model";
    remove.type = "button";
    remove.title = `Remove ${model.name}`;
    remove.setAttribute("aria-label", `Remove downloaded model ${model.name}`);
    remove.disabled = setupJob.status === "running";
    remove.innerHTML = '<img src="/static/icons/delete_24px.svg" alt="" />';
    remove.addEventListener("click", () => requestModelRemoval(model));
    row.append(info, remove);
    container.append(row);
  }
}

function requestModelRemoval(model) {
  pendingDeleteModel = model;
  $("#delete-model-name").textContent = model.name;
  const settings = getSettings();
  const isEmbedding = normalizeModelName(model.name) === normalizeModelName(settings.embedding_model);
  const isAnswer = normalizeModelName(model.name) === normalizeModelName(settings.chat_model);
  let detail = `This removes about ${formatSize(model.size_bytes)} from ${modelSystemProfile?.storage?.model_directory || "the model folder"}. You can download it again later.`;
  if (isEmbedding) detail += " This is your search model. If you switch to another search model, rebuild the library index.";
  if (isAnswer) detail += " This is the selected answer model; choose or download another before asking questions.";
  $("#delete-model-copy").textContent = detail;
  $("#delete-model-dialog").showModal();
}

async function deletePendingModel() {
  if (!pendingDeleteModel) return;
  const button = $("#confirm-delete-model");
  button.disabled = true;
  try {
    const settings = getSettings();
    await api("/api/models/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ollama_url: settings.ollama_url, model: pendingDeleteModel.name }),
    });
    const removedName = pendingDeleteModel.name;
    pendingDeleteModel = null;
    $("#delete-model-dialog").close();
    toast(`${removedName} removed from this computer.`, "success");
    await refreshSystemProfile();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
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
  const installNeeded = ollamaWillBeInstalled();
  const installerBytes = installNeeded ? modelSystemProfile.ollama.installation_download_bytes : 0;
  const totalDownloadBytes = missingBytes + installerBytes;
  const freeBytes = modelSystemProfile.storage.free_bytes;
  const lines = [
    `<strong>${escapeHTML(selected.id)} + ${escapeHTML(selected.embedding_model)}</strong>`,
    `<span>Model files will be stored in ${escapeHTML(modelSystemProfile.storage.model_directory)}.</span>`,
    `<span>Model files after setup: about ${formatModelSize(fullModelStorage)} total. Downloads for this setup: about ${formatModelSize(totalDownloadBytes)} altogether.</span>`,
    `<span>LightWeightRAG and its Python environment currently use ${formatSize(modelSystemProfile.system.app_footprint_bytes)} on this computer; that space is already in use. Your books and index use additional space.</span>`,
    `<span>${escapeHTML(modelSystemProfile.storage.location_label)}: ${formatSize(freeBytes)} free. Keep at least ${formatModelSize(selected.required_free_bytes)} free for this download and temporary headroom.</span>`,
  ];
  if (installNeeded) {
    lines.splice(2, 0, `<span>Ollama installer: about ${formatModelSize(installerBytes)} to download and ${formatModelSize(modelSystemProfile.ollama.installation_storage_bytes)} for the installed runtime. It will be installed for your account.</span>`);
    lines.push(`<span>Ollama install location: ${formatSize(modelSystemProfile.storage.installation_free_bytes)} free. Temporary download location: ${formatSize(modelSystemProfile.storage.installer_temp_free_bytes)} free.</span>`);
    lines.push(`<span>Reserve about ${formatModelSize(selected.required_free_bytes + modelSystemProfile.ollama.installation_storage_bytes + installerBytes)} across these locations for this setup.</span>`);
  }
  estimate.innerHTML = lines.join("");
  $("#setup-consent-copy").textContent = installNeeded
    ? `I reviewed the estimate and want LightWeightRAG to install Ollama and download about ${formatModelSize(totalDownloadBytes)} of files locally.`
    : selected.installed
    ? "I reviewed the estimate and want to use these installed models in this workspace."
    : `I reviewed the estimate and want to download about ${formatModelSize(totalDownloadBytes)} of model files locally.`;
  // Reviewing the estimate is separate from download readiness. Hardware,
  // storage, and Ollama availability are explained below and gate the action.
  consent.disabled = !modelSystemProfile || !selected || setupJob.status === "running";
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
  if (!ollama.model_directory_compatible) {
    setSetupWarning(`Ollama is already running from ${ollama.active_model_directory || "another folder"}. Quit Ollama and refresh before choosing a different model folder.`, "error");
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
  if (!selected.setup_storage_ok) {
    const shortfall = selected.setup_storage_shortfall_bytes === null
      ? "some additional space"
      : `about ${formatModelSize(selected.setup_storage_shortfall_bytes)} more space`;
    setSetupWarning(`There is not enough free space for Ollama and this model setup. Free ${shortfall} across the app, download, and model locations, then refresh the check.`, "error");
    return;
  }
  if (!ollama.running && !ollama.installed && !ollama.install_supported) {
    help.hidden = false;
    setSetupWarning(`LightWeightRAG cannot automatically install Ollama for ${system.os} ${system.architecture}. Use the official installer, then refresh this check.`, "error");
    return;
  }
  if (!ollama.running) {
    setSetupWarning(ollama.installed
      ? "Ollama is installed but not running. LightWeightRAG will start it automatically when you continue."
      : "Ollama is not installed. LightWeightRAG can install it for your account and then set up the selected models in one step.");
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
  else if (ollamaWillBeInstalled()) label.textContent = "Install Ollama and download models";
  else if (modelSystemProfile && !modelSystemProfile.ollama.running) label.textContent = "Start Ollama and set up models";
  else label.textContent = "Download and set up";

  const hint = $("#setup-action-hint");
  let message = "Review the size and storage estimate, then check the box above to continue.";
  if (busy) message = "Model setup is in progress. This panel will update when it finishes.";
  else if (!modelSystemProfile) message = "Wait for the system check to finish before continuing.";
  else if (!selected) message = "No compatible answer model is available for this computer.";
  else if (!selected.ram_ok) message = `This model needs at least ${formatSize(selected.min_ram_bytes)} RAM. Choose a smaller model.`;
  else if (modelSystemProfile.storage.free_bytes === null) message = "Free storage could not be checked, so downloads are paused.";
  else if (!selected.storage_ok) message = `Free about ${formatModelSize(selected.storage_shortfall_bytes)} or choose a smaller model.`;
  else if (!selected.setup_storage_ok) message = selected.setup_storage_shortfall_bytes === null
    ? "Available installation or temporary storage could not be checked."
    : `Free about ${formatModelSize(selected.setup_storage_shortfall_bytes)} more across the Ollama and model locations.`;
  else if (!modelSystemProfile.ollama.running && !modelSystemProfile.ollama.installed && !modelSystemProfile.ollama.install_supported) message = "Automatic Ollama installation is not supported on this computer.";
  else if (!modelSystemProfile.ollama.local_url) message = "Use the local Ollama address http://localhost:11434 to keep setup on this computer.";
  else if (!modelSystemProfile.ollama.running && !modelSystemProfile.ollama.installed) message = "One click will install Ollama for your account, start it, and download the selected models.";
  else if (!modelSystemProfile.ollama.running) message = "One click will start the installed Ollama service and set up the selected models.";
  else if (!$("#setup-consent").checked) message = "Review the size and storage estimate, then check the box above to continue.";
  else message = selected.installed ? "Ready to use these installed models." : "Ready to download the selected models to this computer.";
  hint.textContent = message;
  button.setAttribute("aria-describedby", "setup-estimate setup-warning setup-action-hint");
  button.title = message;
}

function renderSystemProfile(profile) {
  modelSystemProfile = profile;
  if (!$("#model-directory").value.trim()) $("#model-directory").value = profile.storage.model_directory;
  renderInstalledModels(profile);
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
  const installedCompatible = profile.models.filter((model) => model.installed && model.ram_ok);
  const preferred = availablePrevious ? previous : installedCompatible.at(-1)?.id || profile.recommended_model;
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
  renderLibrary(libraryState);
}

function systemMemoryAvailable(profile) {
  return Boolean(profile?.system?.memory_bytes);
}

async function refreshSystemProfile() {
  const url = $("#ollama-url").value.trim() || "http://localhost:11434";
  const directory = $("#model-directory").value.trim();
  modelSystemProfile = null;
  $("#system-summary").textContent = "Checking this computer…";
  $("#setup-model").disabled = true;
  $("#setup-consent").checked = false;
  $("#setup-consent").disabled = true;
  updateSetupButton();
  try {
    const profile = await api(`/api/system?ollama_url=${encodeURIComponent(url)}&model_directory=${encodeURIComponent(directory)}`);
    renderSystemProfile(profile);
    return profile;
  } catch (error) {
    modelSystemProfile = null;
    $("#system-summary").textContent = "System check unavailable.";
    renderSetupEstimate();
    setSetupWarning(error.message, "error");
    updateSetupButton();
    throw error;
  }
}

function renderSetupJob(job) {
  const previousStatus = setupJob.status;
  setupJob = job || { status: "idle" };
  if (previousStatus !== setupJob.status && modelSystemProfile) renderInstalledModels(modelSystemProfile);
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
    const changed = settings.chat_model !== setupJob.chat_model || settings.embedding_model !== setupJob.embedding_model || settings.model_directory !== setupJob.model_directory;
    if (changed) {
      settings.chat_model = setupJob.chat_model;
      settings.embedding_model = setupJob.embedding_model;
      settings.model_directory = setupJob.model_directory || settings.model_directory;
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
    const settings = getSettings();
    settings.model_directory = $("#model-directory").value.trim();
    localStorage.setItem(settingsKey, JSON.stringify(settings));
    const job = await api("/api/models/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ollama_url: $("#ollama-url").value.trim() || "http://localhost:11434",
        chat_model: selected.id,
        model_directory: settings.model_directory,
      }),
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

async function browseForModelFolder() {
  const button = $("#choose-model-directory");
  button.disabled = true;
  try {
    const result = await api("/api/models/choose-folder", { method: "POST" });
    if (!result.cancelled && result.path) {
      $("#model-directory").value = result.path;
      $("#setup-consent").checked = false;
      await refreshSystemProfile();
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function checkModelsOnLaunch() {
  const dialog = $("#settings-dialog");
  try {
    const profile = await refreshSystemProfile();
    if (configuredModelsAreInstalled(profile)) return;
  } catch (_) {
    // If the local setup check cannot be completed, show the troubleshooting
    // panel so the user can see the status and refresh it from one place.
  }

  initializeSettings();
  if (!dialog.open) dialog.showModal();
  loadSetupJob();
}

function openModelSettings() {
  const dialog = $("#settings-dialog");
  initializeSettings();
  if (!dialog.open) dialog.showModal();
  refreshSystemProfile().catch(() => {});
  loadSetupJob();
}

function installHandlers() {
  const picker = $("#file-input");
  const dropzone = $("#dropzone");
  picker.addEventListener("change", () => uploadFiles(picker.files));
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (indexJob.status === "running") toast("Wait for indexing to finish before adding more documents.");
      else picker.click();
    }
  });
  ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); }));
  dropzone.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));
  const projectDialog = $("#create-project-dialog");
  const openCreateProjectDialog = () => {
    $("#project-form-error").hidden = true;
    $("#project-name").value = "";
    $("#new-project-instructions").value = "";
    renderProjectInclusions();
    projectDialog.showModal();
    requestAnimationFrame(() => $("#project-name").focus());
  };
  $("#add-project").addEventListener("click", openCreateProjectDialog);
  $("#project-home-add").addEventListener("click", openCreateProjectDialog);
  $("#browse-projects-sidebar").addEventListener("click", showProjectPicker);
  $("#projects-home-link").addEventListener("click", showProjectPicker);
  $("#project-view-grid").addEventListener("click", () => setProjectView("grid"));
  $("#project-view-list").addEventListener("click", () => setProjectView("list"));
  $("#cancel-create-project").addEventListener("click", () => projectDialog.close());
  $("#create-project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const includedProjectIds = $$("input[name='included-project']:checked", projectDialog).map((input) => input.value);
    createResearchProject($("#project-name").value.trim(), $("#new-project-instructions").value.trim(), includedProjectIds);
  });
  projectDialog.addEventListener("click", (event) => { if (event.target === projectDialog) projectDialog.close(); });
  $("#save-project-instructions").addEventListener("click", saveProjectInstructions);
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
  $("#open-settings").addEventListener("click", openModelSettings);
  $("#refresh-system").addEventListener("click", () => refreshSystemProfile().catch(() => {}));
  $("#ollama-url").addEventListener("change", () => {
    refreshSystemProfile().catch(() => {});
  });
  $("#model-directory").addEventListener("change", () => {
    $("#setup-consent").checked = false;
    refreshSystemProfile().catch(() => {});
  });
  $("#choose-model-directory").addEventListener("click", browseForModelFolder);
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
  $("#confirm-delete-model").addEventListener("click", deletePendingModel);
  $("#cancel-delete-model").addEventListener("click", () => {
    pendingDeleteModel = null;
    $("#delete-model-dialog").close();
  });
  $("#cancel-settings").addEventListener("click", () => dialog.close());
  $("#save-settings").addEventListener("click", () => {
    const settings = modelSettings();
    if (!settings.ollama_url || !settings.chat_model || !settings.embedding_model) { toast("Enter an Ollama address and both model names.", "error"); return; }
    localStorage.setItem(settingsKey, JSON.stringify(settings));
    dialog.close();
    toast("Model settings saved in this browser.", "success");
    refreshProjects();
    refreshSystemProfile().catch(() => {});
  });
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  $("#delete-model-dialog").addEventListener("click", (event) => {
    if (event.target === $("#delete-model-dialog")) {
      pendingDeleteModel = null;
      $("#delete-model-dialog").close();
    }
  });
  $("#delete-model-dialog").addEventListener("close", () => { pendingDeleteModel = null; });
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
loadIndexJob();
refreshProjects();
checkModelsOnLaunch();
