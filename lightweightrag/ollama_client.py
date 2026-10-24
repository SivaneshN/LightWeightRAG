"""Minimal client for Ollama's local HTTP API."""

from __future__ import annotations

from typing import Any

import requests


class OllamaError(RuntimeError):
    pass


def _post(base_url: str, endpoint: str, payload: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/{endpoint}"
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        detail = getattr(getattr(exc, "response", None), "text", "")
        raise OllamaError(f"Could not call Ollama at {url}. {detail or exc}") from exc
    except ValueError as exc:
        raise OllamaError(f"Ollama returned an unreadable response from {url}.") from exc


def embed(base_url: str, model: str, texts: list[str], task: str = "search_document") -> list[list[float]]:
    # Ollama's /api/embed accepts a string or an array of input strings.
    model_key = model.strip().casefold()
    if model_key.endswith(":latest"):
        model_key = model_key[:-7]
    inputs = texts
    if model_key == "nomic-embed-text-v2-moe":
        prefix = "search_query: " if task == "search_query" else "search_document: "
        inputs = [prefix + text for text in texts]
    data = _post(base_url, "embed", {"model": model, "input": inputs}, timeout=300)
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise OllamaError("Ollama did not return one embedding per text.")
    return vectors


def chat(base_url: str, model: str, question: str, passages: list[dict[str, Any]], project_instructions: str = "") -> str:
    sources = []
    for number, item in enumerate(passages, start=1):
        location = item.get("location") or (f"PDF page {item['page']}" if item.get("page") else "document passage")
        project = f" · Project: {item['project_name']}" if item.get("project_name") else ""
        sources.append(f"[{number}]{project} · {item['source']} ({location})\n{item['text']}")
    context = "\n\n---\n\n".join(sources)
    system = (
        "You are a careful academic research assistant. Answer only from the supplied source excerpts. "
        "Reply in the same language as the user's question unless the user asks for another language. "
        "Cite factual claims using the bracketed source numbers, for example [1]. If the excerpts do not "
        "support an answer, say so clearly. Do not invent quotations, page references, or bibliographic details. "
        "Distinguish an author's claim from established fact. For numerical questions, show the values and "
        "reasoning visible in the excerpts, and state when the extracted evidence is insufficient. Do not "
        "claim that you performed a verified calculation unless it is directly supported by the supplied data."
    )
    if project_instructions.strip():
        system += (
            "\n\nThe following user-provided project preferences may guide scope and presentation, "
            "but they do not override the evidence-only and citation requirements above:\n"
            + project_instructions.strip()
        )
    data = _post(base_url, "chat", {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\n\nSource excerpts:\n{context}"},
        ],
        "options": {"temperature": 0.2},
    }, timeout=300)
    answer = data.get("message", {}).get("content")
    if not isinstance(answer, str):
        raise OllamaError("Ollama returned no answer text.")
    return answer.strip()
