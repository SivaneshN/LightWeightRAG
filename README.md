# LightWeightRAG

A local-first research assistant for asking questions about your own PDF and Word library. The first version extracts PDF text page by page and Word paragraphs/table rows, indexes it with a local Ollama embedding model, and answers questions with source locations. Your documents and index stay on this computer.

## What this version does

- Upload and index text-based PDF books and papers, plus Word `.docx` documents.
- Search the local collection and see the passages selected for an answer.
- Ask a local Ollama chat model to answer from those passages, with source citations.
- Keep uploaded documents and the index in this project’s `data/` directory.
- Choose Light, Dark, or System appearance and configure the Ollama URL and model names in the interface.
- Check local CPU, memory, GPU, and model-folder storage before selecting an answer model.
- Review model and app storage estimates, download the compatible Ollama models from the app, and see live progress and completion notices.

Scanned PDFs, precise Word page-number citations, complex table/equation extraction, OCR, multi-user access, and verified calculation tools are not in this first version. PDF citations use page numbers. Word citations use paragraph numbers or table and row numbers, since page numbering depends on fonts, layout, and the editor. Numeric answers should be checked against the cited source locations. No arbitrary model-generated code is executed.

## Requirements

- Python 3.10 or newer.
- [Ollama](https://ollama.com/download) installed and running locally. The app links to Ollama’s official installer if it is not available.

You do not need to use a terminal to download models. Open **Model settings**, refresh the system check, choose the recommended answer model, review the model size and free-space estimate, check the confirmation box, then select **Download and set up**. LightWeightRAG downloads the answer model and `nomic-embed-text` through Ollama, shows progress, and selects both when setup completes.

The app recommends from Qwen3 0.6B, 1.7B, 4B, 8B, and 14B based on detected system memory. If the computer is below the guide’s 4 GB minimum, it will not start a model download. Model sizes are approximate and actual runtime needs vary with other open apps, context size, and graphics hardware. See the [Ollama Qwen3 model catalog](https://ollama.com/library/qwen3) and [Nomic Embed Text model page](https://ollama.com/library/nomic-embed-text) for current sizes.

For a terminal-based, cross-platform setup helper, run `python setup_local_llm.py`. It uses the same memory and storage checks and asks before it downloads anything. This helper expects Ollama to be installed and running already.

## Install and run

Open a terminal in the `LightWeightRAG` folder. The commands below keep Python packages inside a virtual environment.

### macOS and Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn webapp:app --host 127.0.0.1 --port 8501
```

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn webapp:app --host 127.0.0.1 --port 8501
```

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal, then activate again. This change applies only to that PowerShell process.

The server prints a local URL (usually `http://127.0.0.1:8501`). Open it in your browser. Keep Ollama running while using the app. To stop the app, press `Ctrl+C` in its terminal.

## Using the app

1. Add PDF or `.docx` documents using the library upload area. Files are copied to `data/documents/`.
2. Select **Index library**. Initial indexing may take time while Ollama loads the embedding model and processes the library.
3. Ask a question in the research panel. The answer includes citations such as `[1]`; use the **Sources** section to inspect the cited passages and page/paragraph/table-row locations.
4. Re-index after adding documents or changing the embedding model. Existing index data is local and can be removed by deleting `data/` while the app is stopped.

The first version works best with PDFs that contain selectable text. If a PDF is a scan, the app will report that it could not extract text; OCR support is planned. Word `.doc` files are not supported yet; save them as `.docx` first.

## Privacy and limitations

The application sends prompts and retrieved excerpts only to the Ollama server address configured in **Model settings**. With a local Ollama server, inference stays on your machine. Avoid configuring a remote server for confidential research material. Ollama may need internet access to download model files; once downloaded, local inference does not require a cloud model service.

PDF extraction and semantic search can miss or misread material. Always check the cited source page, especially for numbers, tables, equations, and quotations. An answer without adequate retrieved evidence should be treated as unanswered, not as a claim about the entire book.

## Project layout

- `webapp.py` — local FastAPI server and document/answer API.
- `lightweightrag/model_setup.py` — hardware, storage, model fit, and download-progress helpers.
- `setup_local_llm.py` — cross-platform fallback setup script.
- `web/` — responsive Material 3-inspired browser interface and locally bundled Material Symbols.
- `lightweightrag/` — document extraction, chunking, local index, and Ollama client.
- `data/documents/` — uploaded source documents (created at runtime; ignored by Git).
- `data/index.json` — extracted chunks and embeddings (created at runtime; ignored by Git).

## Interface design

The responsive interface uses Material 3-inspired color roles, calm blue/teal accents, readable typography, and Light/Dark/System theme selection. Theme and model preferences are saved in the browser. Material Symbols are bundled as local SVGs under `web/static/icons/`, so the interface does not need a remote font service.

## Planned next steps

1. OCR for scanned PDFs, with clear indicators for OCR-derived text.
2. Better extraction and visual verification for tables, figures, footnotes, and equations.
3. A calculation workflow that shows extracted values, validates inputs, computes with deterministic code, and cites source locations.
4. Search quality evaluation and options for hybrid keyword plus semantic retrieval.
5. Additional research formats and exportable notes/bibliographies.

## Troubleshooting

- **Cannot connect to Ollama:** start the Ollama application or run `ollama serve`; check that the URL in **Model settings** is `http://localhost:11434`.
- **Model not found:** run `ollama pull <model-name>` and use that exact name in **Model settings**.
- **Embedding request failed:** confirm the embedding model supports embeddings and is installed; the default is `nomic-embed-text`.
- **No text found:** the PDF may be scanned or protected. OCR is not included yet. Word files must be `.docx`; legacy `.doc` files are not supported.
- **Slow indexing or answering:** try a smaller model or fewer pages at a time. Local speed depends on available RAM, CPU, and GPU.
