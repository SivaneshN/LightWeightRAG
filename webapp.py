"""Local FastAPI server for the LightWeightRAG browser interface."""

from __future__ import annotations

import math
import json
import os
import re
import sys
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lightweightrag.library import chunk_units, cosine_top_k, extract_docx, extract_pdf, load_index, save_index
from lightweightrag.model_setup import DEFAULT_OLLAMA_URL, choose_model_directory, get_system_profile, setup_manager, validate_model_directory
from lightweightrag.ollama_client import OllamaError, chat, embed


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("LIGHTWEIGHTRAG_DATA_DIR", ROOT / "data")).expanduser()
PROJECT_DIR = DATA_DIR / "projects"
PROJECTS_PATH = DATA_DIR / "projects.json"
LEGACY_TOPIC_DIR = DATA_DIR / "topics"
LEGACY_TOPICS_PATH = DATA_DIR / "topics.json"
DOCUMENT_DIR = DATA_DIR / "documents"
INDEX_PATH = DATA_DIR / "index.json"
WEB_DIR = ROOT / "web"
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_LOCK = threading.RLock()


def save_projects(projects: list[dict[str, Any]]) -> None:
    temporary_path = PROJECTS_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps({"projects": projects}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(PROJECTS_PATH)


def migrate_legacy_projects() -> None:
    with PROJECT_LOCK:
        if not PROJECTS_PATH.exists():
            stored = []
            if LEGACY_TOPICS_PATH.exists():
                try:
                    stored = json.loads(LEGACY_TOPICS_PATH.read_text(encoding="utf-8")).get("topics", [])
                except (OSError, json.JSONDecodeError, AttributeError):
                    stored = []
            migrated = []
            for item in stored:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str):
                    migrated.append({"id": item["id"], "name": item["name"].strip(), "instructions": str(item.get("instructions", ""))})
            if not any(item.get("id") == "general" for item in migrated):
                migrated.insert(0, {"id": "general", "name": "General", "instructions": ""})
            save_projects(migrated)

        for project in list_projects():
            if project["id"] == "general":
                continue
            legacy_folder = LEGACY_TOPIC_DIR / project["id"]
            project_folder = PROJECT_DIR / project["id"]
            if legacy_folder.exists() and not project_folder.exists():
                legacy_folder.replace(project_folder)


def list_projects() -> list[dict[str, Any]]:
    with PROJECT_LOCK:
        if not PROJECTS_PATH.exists():
            save_projects([{"id": "general", "name": "General", "instructions": ""}])
        try:
            stored = json.loads(PROJECTS_PATH.read_text(encoding="utf-8")).get("projects", [])
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError("The local project list could not be read.") from exc
        if not isinstance(stored, list):
            raise RuntimeError("The local project list has an invalid format.")
        projects = []
        seen_ids = set()
        for item in stored:
            if not isinstance(item, dict):
                continue
            project_id = item.get("id")
            name = item.get("name")
            instructions = item.get("instructions", "")
            if not isinstance(project_id, str) or not isinstance(name, str) or not name.strip():
                continue
            if project_id != "general" and not re.fullmatch(r"(?:topic|project)-[a-f0-9]{12}", project_id):
                continue
            if project_id not in seen_ids:
                included = item.get("included_project_ids", [])
                projects.append({
                    "id": project_id,
                    "name": name.strip(),
                    "instructions": instructions if isinstance(instructions, str) else "",
                    "included_project_ids": included if isinstance(included, list) else [],
                })
                seen_ids.add(project_id)
        if "general" not in seen_ids:
            projects.insert(0, {"id": "general", "name": "General", "instructions": "", "included_project_ids": []})
            save_projects(projects)
        valid_ids = {project["id"] for project in projects}
        for project in projects:
            included_ids = project["included_project_ids"]
            project["included_project_ids"] = list(dict.fromkeys(
                project_id for project_id in included_ids
                if isinstance(project_id, str) and project_id in valid_ids and project_id != project["id"]
            ))
        return projects


def get_project(project_id: str) -> dict[str, Any]:
    project = next((item for item in list_projects() if item["id"] == project_id), None)
    if project is None:
        raise ValueError("That project does not exist.")
    return project


def project_context_ids(project_id: str) -> list[str]:
    project = get_project(project_id)
    return list(dict.fromkeys([project["id"], *project.get("included_project_ids", [])]))


def project_folder(project_id: str) -> Path:
    get_project(project_id)
    return DATA_DIR if project_id == "general" else PROJECT_DIR / project_id


def project_documents_folder(project_id: str) -> Path:
    return DOCUMENT_DIR if project_id == "general" else project_folder(project_id) / "documents"


def project_index_file(project_id: str) -> Path:
    return INDEX_PATH if project_id == "general" else project_folder(project_id) / "index.json"


def create_project(name: str, instructions: str = "", included_project_ids: list[str] | None = None) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Enter a name for this project.")
    if len(clean_name) > 80:
        raise ValueError("Project names can be up to 80 characters.")
    with PROJECT_LOCK:
        projects = list_projects()
        if any(item["name"].casefold() == clean_name.casefold() for item in projects):
            raise ValueError("A project with that name already exists.")
        existing_ids = {item["id"] for item in projects}
        requested_ids = list(dict.fromkeys(included_project_ids or []))
        unknown_ids = [project_id for project_id in requested_ids if project_id not in existing_ids]
        if unknown_ids:
            raise ValueError("Choose existing projects from the list to include.")
        project = {
            "id": f"project-{uuid.uuid4().hex[:12]}",
            "name": clean_name,
            "instructions": instructions.strip(),
            "included_project_ids": requested_ids,
        }
        (PROJECT_DIR / project["id"] / "documents").mkdir(parents=True, exist_ok=True)
        save_projects([*projects, project])
        return project


migrate_legacy_projects()
for stored_project in list_projects():
    project_documents_folder(stored_project["id"]).mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LightWeightRAG", docs_url=None, redoc_url=None)
DESKTOP_API_TOKEN = os.environ.get("LIGHTWEIGHTRAG_DESKTOP_TOKEN", "")


@app.middleware("http")
async def require_desktop_api_token(request, call_next):
    """Keep the packaged loopback API private to its desktop webview."""
    if DESKTOP_API_TOKEN and request.url.path.startswith("/api/"):
        if request.headers.get("x-lightweightrag-desktop-token") != DESKTOP_API_TOKEN:
            return JSONResponse(status_code=403, content={"detail": "This local app request is not authorized."})
    return await call_next(request)


class ModelSettings(BaseModel):
    ollama_url: str = Field(default="http://localhost:11434", max_length=500)
    chat_model: str = Field(default="qwen3:4b", max_length=200)
    embedding_model: str = Field(default="nomic-embed-text-v2-moe", max_length=200)


class QuestionRequest(ModelSettings):
    question: str = Field(min_length=1, max_length=10000)
    project_id: str = "general"


class IndexRequest(ModelSettings):
    project_id: str = "general"


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(default="", max_length=5000)
    included_project_ids: list[str] = Field(default_factory=list, max_length=100)


class ProjectInstructionsRequest(BaseModel):
    instructions: str = Field(default="", max_length=5000)


class ModelSetupRequest(BaseModel):
    ollama_url: str = Field(default=DEFAULT_OLLAMA_URL, max_length=500)
    chat_model: str = Field(min_length=1, max_length=100)
    model_directory: str | None = Field(default=None, max_length=2000)


class ModelDeleteRequest(BaseModel):
    ollama_url: str = Field(default=DEFAULT_OLLAMA_URL, max_length=500)
    model: str = Field(min_length=1, max_length=200)


class IndexJobManager:
    """Run one library indexing job and expose progress for the browser UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"status": "idle", "progress": 0}

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = deepcopy(self._state)
        batch_started_at = state.pop("_active_batch_started_at", None)
        average_batch_seconds = state.pop("_average_batch_seconds", None)
        if (
            state.get("status") == "running"
            and state.get("phase") == "Building multilingual search index"
            and batch_started_at is not None
            and average_batch_seconds
            and state.get("total_batches")
        ):
            completed_batches = int(state.get("completed_batches", 0))
            total_batches = int(state["total_batches"])
            elapsed = max(0.0, time.monotonic() - batch_started_at)
            current_batch_fraction = min(0.9, elapsed / average_batch_seconds)
            estimated_batches_done = completed_batches + current_batch_fraction
            state["progress"] = max(
                int(state.get("progress", 0)),
                min(97, 35 + int(63 * estimated_batches_done / total_batches)),
            )
            state["eta_seconds"] = max(
                1,
                math.ceil(average_batch_seconds * (total_batches - estimated_batches_done)),
            )
            state["message"] = (
                f"Embedding batch {completed_batches + 1} of {total_batches} · "
                f"{int(state.get('completed_passages', 0)):,} passages completed so far."
            )
        return state

    def is_running(self) -> bool:
        with self._lock:
            return self._state.get("status") == "running"

    def start(self, settings: ModelSettings, project_ids: list[str]) -> dict[str, Any]:
        projects = []
        for project_id in dict.fromkeys(project_ids):
            project = get_project(project_id)
            folder = project_documents_folder(project_id)
            paths = sorted([*folder.glob("*.pdf"), *folder.glob("*.docx")], key=lambda item: item.name.casefold())
            if paths:
                projects.append({**project, "paths": paths})
        total_documents = sum(len(project["paths"]) for project in projects)
        if not total_documents:
            raise ValueError("Add documents to an included project before indexing.")
        with self._lock:
            if self._state.get("status") == "running":
                raise ValueError("Indexing is already in progress.")
            job_id = uuid.uuid4().hex
            self._state = {
                "job_id": job_id,
                "status": "running",
                "progress": 0,
                "phase": "Preparing your library",
                "message": f"Preparing {total_documents} document{'s' if total_documents != 1 else ''} across {len(projects)} project{'s' if len(projects) != 1 else ''}…",
                "project_ids": [project["id"] for project in projects],
                "project_names": [project["name"] for project in projects],
                "document_number": 0,
                "document_count": total_documents,
                "current_document": None,
                "unit_label": None,
                "current_unit": 0,
                "total_units": 0,
                "completed_passages": 0,
                "total_passages": 0,
                "completed_batches": 0,
                "total_batches": 0,
                "eta_seconds": None,
                "indexed_count": 0,
                "problems": [],
                "error": None,
            }
        thread = threading.Thread(
            target=self._run,
            args=(job_id, projects, settings.ollama_url, settings.embedding_model),
            daemon=True,
        )
        thread.start()
        return self.status()

    def _update(self, job_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            if self._state.get("job_id") == job_id:
                self._state.update(values)

    def _run(self, job_id: str, projects: list[dict[str, Any]], ollama_url: str, embedding_model: str) -> None:
        started_at = time.monotonic()
        chunks: list[dict[str, Any]] = []
        problems: list[str] = []
        files = [(project, path) for project in projects for path in project["paths"]]
        total_documents = len(files)
        try:
            for document_index, (project, path) in enumerate(files, start=1):
                is_pdf = path.suffix.lower() == ".pdf"
                unit_label = "PDF page" if is_pdf else "Word paragraph or table row"

                def report_unit(completed: int, total: int) -> None:
                    fraction = completed / total if total else 1.0
                    progress = int(30 * ((document_index - 1 + fraction) / total_documents))
                    self._update(job_id, {
                        "progress": max(1, min(30, progress)),
                        "phase": f"Reading document {document_index} of {total_documents}",
                        "message": f"{project['name']} · {path.name} · {unit_label} {completed} of {total}",
                        "document_number": document_index,
                        "current_project": project["name"],
                        "current_document": path.name,
                        "unit_label": unit_label,
                        "current_unit": completed,
                        "total_units": total,
                    })

                self._update(job_id, {
                    "progress": int(30 * (document_index - 1) / total_documents),
                    "phase": f"Reading document {document_index} of {total_documents}",
                    "message": f"{project['name']} · Opening {path.name}…",
                    "current_project": project["name"],
                    "document_number": document_index,
                    "current_document": path.name,
                    "unit_label": unit_label,
                    "current_unit": 0,
                    "total_units": 0,
                })
                try:
                    units = extract_pdf(path, on_page=report_unit) if is_pdf else extract_docx(path, on_unit=report_unit)
                    document_chunks = chunk_units(units, path.name)
                    if document_chunks:
                        for passage in document_chunks:
                            passage["project_id"] = project["id"]
                            passage["project_name"] = project["name"]
                        chunks.extend(document_chunks)
                    else:
                        problems.append(f"{project['name']} · {path.name}: no readable text was found. Scanned PDF OCR is not included yet.")
                except Exception as exc:
                    problems.append(f"{project['name']} · {path.name}: {exc}")

            self._update(job_id, {
                "progress": 35,
                "phase": "Preparing searchable passages",
                "message": f"Prepared {len(chunks):,} passages from {total_documents} document{'s' if total_documents != 1 else ''} across {len(projects)} project{'s' if len(projects) != 1 else ''}.",
                "current_document": None,
                "unit_label": None,
                "current_unit": 0,
                "total_units": 0,
                "total_passages": len(chunks),
                "problems": problems,
            })
            if not chunks:
                detail = "No readable text was found in the library. Scanned PDFs need OCR, which is not included yet."
                self._update(job_id, {
                    "status": "error",
                    "phase": "Indexing could not continue",
                    "error": detail,
                    "message": detail,
                    "problems": problems,
                })
                return

            records: list[dict[str, Any]] = []
            batch_size = 8
            total_batches = math.ceil(len(chunks) / batch_size)
            average_batch_seconds: float | None = None
            self._update(job_id, {
                "total_batches": total_batches,
                "completed_batches": 0,
                "completed_passages": 0,
            })
            for batch_number, offset in enumerate(range(0, len(chunks), batch_size), start=1):
                batch = chunks[offset:offset + batch_size]
                batch_started_at = time.monotonic()
                self._update(job_id, {
                    "phase": "Building multilingual search index",
                    "message": f"Embedding batch {batch_number} of {total_batches}…",
                    "_active_batch_started_at": batch_started_at,
                })
                vectors = embed(ollama_url, embedding_model, [item["text"] for item in batch], task="search_document")
                batch_seconds = time.monotonic() - batch_started_at
                average_batch_seconds = (
                    batch_seconds if average_batch_seconds is None
                    else (average_batch_seconds * (batch_number - 1) + batch_seconds) / batch_number
                )
                for item, vector in zip(batch, vectors):
                    item["embedding"] = vector
                    item["embedding_model"] = embedding_model
                    records.append(item)
                completed_passages = len(records)
                remaining_batches = total_batches - batch_number
                eta_seconds = max(1, math.ceil(average_batch_seconds * remaining_batches)) if remaining_batches else None
                progress = 35 + int(63 * batch_number / total_batches)
                self._update(job_id, {
                    "progress": min(98, progress),
                    "phase": "Building multilingual search index",
                    "message": f"Embedded {completed_passages:,} of {len(chunks):,} passages · batch {batch_number} of {total_batches}",
                    "completed_passages": completed_passages,
                    "completed_batches": batch_number,
                    "total_batches": total_batches,
                    "eta_seconds": eta_seconds,
                    "_active_batch_started_at": None,
                    "_average_batch_seconds": average_batch_seconds,
                })

            self._update(job_id, {"progress": 99, "phase": "Saving your research projects", "message": "Saving the project-specific search indexes locally…"})
            project_counts = {}
            for project in projects:
                project_records = [record for record in records if record.get("project_id") == project["id"]]
                project_counts[project["id"]] = len(project_records)
                index_path = project_index_file(project["id"])
                if project_records:
                    save_index(index_path, project_records)
                else:
                    index_path.unlink(missing_ok=True)
            elapsed_seconds = int(time.monotonic() - started_at)
            message = f"Indexed {len(records):,} passages across {len(projects)} project{'s' if len(projects) != 1 else ''} in {elapsed_seconds} seconds."
            if problems:
                message += f" {len(problems)} document{'s' if len(problems) != 1 else ''} had an issue."
            self._update(job_id, {
                "status": "complete",
                "progress": 100,
                "phase": "Indexing complete",
                "message": message,
                "indexed_count": len(records),
                "project_counts": project_counts,
                "indexed_project_ids": [project_id for project_id, count in project_counts.items() if count],
                "completed_passages": len(records),
                "total_passages": len(chunks),
                "completed_batches": total_batches,
                "total_batches": total_batches,
                "eta_seconds": 0,
                "elapsed_seconds": elapsed_seconds,
                "problems": problems,
            })
        except Exception as exc:
            self._update(job_id, {
                "status": "error",
                "phase": "Indexing needs attention",
                "error": str(exc),
                "message": str(exc),
                "problems": problems,
                "eta_seconds": None,
            })


index_manager = IndexJobManager()


@app.get("/api/system")
def system_profile(ollama_url: str = DEFAULT_OLLAMA_URL, model_directory: str | None = None):
    try:
        return get_system_profile(ollama_url, model_directory)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/models/choose-folder")
def choose_model_folder():
    try:
        selected = choose_model_directory()
        if not selected:
            return {"path": None, "cancelled": True}
        return {"path": str(validate_model_directory(selected)), "cancelled": False}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/models/setup/status")
def model_setup_status():
    return setup_manager.status()


@app.post("/api/models/setup")
def start_model_setup(request: ModelSetupRequest):
    try:
        return setup_manager.start(request.ollama_url, request.chat_model, request.model_directory)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/models/delete")
def delete_model(request: ModelDeleteRequest):
    try:
        setup_manager.delete_model(request.ollama_url, request.model)
        return {"deleted": request.model}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def safe_filename(raw_name: str) -> str:
    name = raw_name.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or "\x00" in name:
        raise HTTPException(status_code=400, detail="Please choose a valid filename.")
    return name


def project_summary(project: dict[str, Any]) -> dict[str, Any]:
    documents_dir = project_documents_folder(project["id"])
    records = load_index(project_index_file(project["id"]))
    embedding_models = {record.get("embedding_model") for record in records}
    return {
        **project,
        "document_count": len([*documents_dir.glob("*.pdf"), *documents_dir.glob("*.docx")]),
        "indexed_count": len(records),
        "indexed": bool(records),
        "embedding_model": next(iter(embedding_models)) if len(embedding_models) == 1 else None,
    }


@app.get("/api/projects")
def get_projects():
    return {"projects": [project_summary(project) for project in list_projects()]}


@app.post("/api/projects")
def add_project(request: ProjectCreateRequest):
    try:
        project = create_project(request.name, request.instructions, request.included_project_ids)
        return {"project": project_summary(project)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/projects/{project_id}")
def update_project_instructions(project_id: str, request: ProjectInstructionsRequest):
    try:
        get_project(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with PROJECT_LOCK:
        projects = list_projects()
        for project in projects:
            if project["id"] == project_id:
                project["instructions"] = request.instructions.strip()
                break
        save_projects(projects)
    return {"project": project_summary(get_project(project_id))}


@app.get("/api/library")
def get_library(project_id: str = "general"):
    try:
        project = get_project(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    documents_dir = project_documents_folder(project_id)
    records = load_index(project_index_file(project_id))
    embedding_models = {record.get("embedding_model") for record in records}
    indexed_sources = {item.get("source") for item in records}
    documents = []
    for path in sorted([*documents_dir.glob("*.pdf"), *documents_dir.glob("*.docx")], key=lambda item: item.name.casefold()):
        documents.append({
            "name": path.name,
            "kind": "PDF" if path.suffix.lower() == ".pdf" else "Word",
            "size": path.stat().st_size,
            "indexed": path.name in indexed_sources,
        })
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "documents": documents,
        "indexed_count": len(records),
        "indexed": bool(records),
        "embedding_model": next(iter(embedding_models)) if len(embedding_models) == 1 else None,
    }


@app.post("/api/documents")
async def upload_documents(files: list[UploadFile] = File(...), project_id: str = "general"):
    if index_manager.is_running():
        raise HTTPException(status_code=409, detail="Wait for indexing to finish before adding documents.")
    try:
        get_project(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    documents_dir = project_documents_folder(project_id)
    saved: list[str] = []
    skipped: list[str] = []
    for upload in files:
        name = safe_filename(upload.filename or "")
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            skipped.append(f"{name}: choose a PDF or Word .docx file.")
            continue
        content = await upload.read()
        if not content:
            skipped.append(f"{name}: the file is empty.")
            continue
        if len(content) > 100 * 1024 * 1024:
            skipped.append(f"{name}: files must be 100 MB or smaller.")
            continue
        (documents_dir / name).write_bytes(content)
        saved.append(name)
    if saved:
        project_index_file(project_id).unlink(missing_ok=True)
    return {"saved": saved, "skipped": skipped}


@app.delete("/api/documents/{filename}")
def delete_document(filename: str, project_id: str = "general"):
    if index_manager.is_running():
        raise HTTPException(status_code=409, detail="Wait for indexing to finish before removing documents.")
    name = safe_filename(filename)
    try:
        get_project(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    target = project_documents_folder(project_id) / name
    if target.suffix.lower() not in {".pdf", ".docx"} or not target.is_file():
        raise HTTPException(status_code=404, detail="That document is not in the library.")
    target.unlink()
    project_index_file(project_id).unlink(missing_ok=True)
    return {"deleted": name}


@app.post("/api/index")
def index_documents(settings: IndexRequest):
    try:
        return index_manager.start(settings, project_context_ids(settings.project_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/index/status")
def index_status():
    return index_manager.status()


@app.post("/api/ask")
def ask_library(request: QuestionRequest):
    if index_manager.is_running():
        raise HTTPException(status_code=409, detail="Your library is being indexed. Wait for indexing to finish before asking a question.")
    records = []
    missing_projects = []
    project_instructions = []
    try:
        context_ids = project_context_ids(request.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    for project_id in context_ids:
        try:
            project = get_project(project_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if project.get("instructions", "").strip():
            project_instructions.append(f"Project “{project['name']}”: {project['instructions'].strip()}")
        documents_dir = project_documents_folder(project_id)
        if not [*documents_dir.glob("*.pdf"), *documents_dir.glob("*.docx")]:
            continue
        project_records = load_index(project_index_file(project_id))
        if not project_records:
            missing_projects.append(project["name"])
            continue
        if any(record.get("embedding_model") != request.embedding_model for record in project_records):
            raise HTTPException(status_code=409, detail=f"The search model changed for “{project['name']}”. Re-index this project's sources before asking questions.")
        for record in project_records:
            record.setdefault("project_id", project_id)
            record.setdefault("project_name", project["name"])
        records.extend(project_records)
    if missing_projects:
        raise HTTPException(status_code=409, detail=f"Index this project's sources before asking: {', '.join(missing_projects)}.")
    if not records:
        raise HTTPException(status_code=400, detail="Select at least one project that contains indexed documents.")
    if any(record.get("embedding_model") != request.embedding_model for record in records):
        raise HTTPException(status_code=409, detail="The search model changed. Re-index your library before asking questions.")
    try:
        vector = embed(request.ollama_url, request.embedding_model, [request.question.strip()], task="search_query")[0]
        sources = cosine_top_k(vector, records, k=6)
        if not sources:
            raise HTTPException(status_code=409, detail="No compatible passages found. Re-index the library using the selected embedding model.")
        answer = chat(request.ollama_url, request.chat_model, request.question.strip(), sources, "\n".join(project_instructions))
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "answer": answer,
        "sources": [
            {
                "number": index,
                "project": item.get("project_name", "General"),
                "source": item["source"],
                "location": item.get("location") or (f"PDF page {item['page']}" if item.get("page") else "document passage"),
                "text": item["text"],
                "score": item["score"],
            }
            for index, item in enumerate(sources, start=1)
        ],
    }


@app.post("/api/desktop/shutdown")
def shutdown_desktop_service(request: Request):
    if not DESKTOP_API_TOKEN:
        raise HTTPException(status_code=404, detail="Not found.")
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="The local service is not ready to stop.")
    server.should_exit = True
    return {"status": "stopping"}


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
