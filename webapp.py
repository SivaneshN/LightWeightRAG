"""Local FastAPI server for the LightWeightRAG browser interface."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lightweightrag.library import chunk_units, cosine_top_k, extract_docx, extract_pdf, load_index, save_index
from lightweightrag.model_setup import DEFAULT_OLLAMA_URL, get_system_profile, setup_manager
from lightweightrag.ollama_client import OllamaError, chat, embed


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCUMENT_DIR = DATA_DIR / "documents"
INDEX_PATH = DATA_DIR / "index.json"
WEB_DIR = ROOT / "web"
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LightWeightRAG", docs_url=None, redoc_url=None)


class ModelSettings(BaseModel):
    ollama_url: str = Field(default="http://localhost:11434", max_length=500)
    chat_model: str = Field(default="qwen2.5:7b", max_length=200)
    embedding_model: str = Field(default="nomic-embed-text", max_length=200)


class QuestionRequest(ModelSettings):
    question: str = Field(min_length=1, max_length=10000)


class ModelSetupRequest(BaseModel):
    ollama_url: str = Field(default=DEFAULT_OLLAMA_URL, max_length=500)
    chat_model: str = Field(min_length=1, max_length=100)


@app.get("/api/system")
def system_profile(ollama_url: str = DEFAULT_OLLAMA_URL):
    return get_system_profile(ollama_url)


@app.get("/api/models/setup/status")
def model_setup_status():
    return setup_manager.status()


@app.post("/api/models/setup")
def start_model_setup(request: ModelSetupRequest):
    try:
        return setup_manager.start(request.ollama_url, request.chat_model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def safe_filename(raw_name: str) -> str:
    name = raw_name.replace("\\", "/").split("/")[-1].strip()
    if not name or name in {".", ".."} or "\x00" in name:
        raise HTTPException(status_code=400, detail="Please choose a valid filename.")
    return name


@app.get("/api/library")
def get_library():
    records = load_index(INDEX_PATH)
    indexed_sources = {item.get("source") for item in records}
    documents = []
    for path in sorted([*DOCUMENT_DIR.glob("*.pdf"), *DOCUMENT_DIR.glob("*.docx")], key=lambda item: item.name.casefold()):
        documents.append({
            "name": path.name,
            "kind": "PDF" if path.suffix.lower() == ".pdf" else "Word",
            "size": path.stat().st_size,
            "indexed": path.name in indexed_sources,
        })
    return {"documents": documents, "indexed_count": len(records), "indexed": bool(records)}


@app.post("/api/documents")
async def upload_documents(files: list[UploadFile] = File(...)):
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
        (DOCUMENT_DIR / name).write_bytes(content)
        saved.append(name)
    if saved:
        INDEX_PATH.unlink(missing_ok=True)
    return {"saved": saved, "skipped": skipped}


@app.delete("/api/documents/{filename}")
def delete_document(filename: str):
    name = safe_filename(filename)
    target = DOCUMENT_DIR / name
    if target.suffix.lower() not in {".pdf", ".docx"} or not target.is_file():
        raise HTTPException(status_code=404, detail="That document is not in the library.")
    target.unlink()
    INDEX_PATH.unlink(missing_ok=True)
    return {"deleted": name}


@app.post("/api/index")
def index_documents(settings: ModelSettings):
    paths = sorted([*DOCUMENT_DIR.glob("*.pdf"), *DOCUMENT_DIR.glob("*.docx")], key=lambda item: item.name.casefold())
    if not paths:
        raise HTTPException(status_code=400, detail="Add a PDF or Word document before indexing.")

    chunks: list[dict] = []
    problems: list[str] = []
    for path in paths:
        try:
            units = extract_pdf(path) if path.suffix.lower() == ".pdf" else extract_docx(path)
            document_chunks = chunk_units(units, path.name)
            if document_chunks:
                chunks.extend(document_chunks)
            else:
                problems.append(f"{path.name}: no text was found. Scanned PDF OCR is not included yet.")
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")

    if not chunks:
        raise HTTPException(status_code=422, detail={"message": "No readable text was found in the library.", "problems": problems})

    try:
        records: list[dict] = []
        batch_size = 24
        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset:offset + batch_size]
            vectors = embed(settings.ollama_url, settings.embedding_model, [item["text"] for item in batch])
            for item, vector in zip(batch, vectors):
                item["embedding"] = vector
                records.append(item)
        save_index(INDEX_PATH, records)
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"indexed_count": len(records), "document_count": len(paths), "problems": problems}


@app.post("/api/ask")
def ask_library(request: QuestionRequest):
    records = load_index(INDEX_PATH)
    if not records:
        raise HTTPException(status_code=400, detail="Index your library before asking a question.")
    try:
        vector = embed(request.ollama_url, request.embedding_model, [request.question.strip()])[0]
        sources = cosine_top_k(vector, records, k=6)
        if not sources:
            raise HTTPException(status_code=409, detail="No compatible passages found. Re-index the library using the selected embedding model.")
        answer = chat(request.ollama_url, request.chat_model, request.question.strip(), sources)
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "answer": answer,
        "sources": [
            {
                "number": index,
                "source": item["source"],
                "location": item.get("location") or (f"PDF page {item['page']}" if item.get("page") else "document passage"),
                "text": item["text"],
                "score": item["score"],
            }
            for index, item in enumerate(sources, start=1)
        ],
    }


@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
