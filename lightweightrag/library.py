"""Page-aware PDF ingestion and a small persistent local vector index."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from pypdf import PdfReader
from docx import Document


def clean_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf(path: Path) -> list[dict[str, Any]]:
    reader = PdfReader(str(path))
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = clean_text(page.extract_text() or "")
        if text:
            pages.append({"location": f"PDF page {page_number}", "text": text})
    return pages


def extract_docx(path: Path) -> list[dict[str, Any]]:
    """Extract Word paragraphs and table rows with stable structural locators."""
    document = Document(str(path))
    units: list[dict[str, Any]] = []
    paragraph_number = 0
    table_number = 0
    for element in document.iter_inner_content():
        if hasattr(element, "rows") and hasattr(element, "columns"):
            table_number += 1
            for row_number, row in enumerate(element.rows, start=1):
                text = " | ".join(clean_text(cell.text) for cell in row.cells if clean_text(cell.text))
                if text:
                    units.append({"location": f"Table {table_number}, row {row_number}", "text": text})
        else:
            paragraph_number += 1
            text = clean_text(element.text)
            if text:
                units.append({"location": f"Paragraph {paragraph_number}", "text": text})
    return units


def chunk_units(pages: list[dict[str, Any]], source: str, size: int = 1300, overlap: int = 180) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for item in pages:
        text = item["text"]
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                boundary = text.rfind(" ", start + size // 2, end)
                if boundary > start:
                    end = boundary
            excerpt = text[start:end].strip()
            if excerpt:
                chunks.append({"source": source, "location": item["location"], "text": excerpt})
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
    return chunks


def load_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_index(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def cosine_top_k(query_vector: list[float], records: list[dict[str, Any]], k: int = 6) -> list[dict[str, Any]]:
    if not records:
        return []
    q = np.asarray(query_vector, dtype=np.float32)
    qnorm = np.linalg.norm(q)
    if not qnorm:
        return []
    candidates = []
    for record in records:
        vector = np.asarray(record.get("embedding", []), dtype=np.float32)
        if vector.shape != q.shape:
            continue
        denom = qnorm * np.linalg.norm(vector)
        score = float(np.dot(q, vector) / denom) if denom else 0.0
        candidates.append((score, record))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [{**record, "score": score} for score, record in candidates[:k]]
