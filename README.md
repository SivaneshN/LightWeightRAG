# LightWeightRAG

A local-first research assistant for asking questions about your own PDF and Word library. The first version extracts PDF text page by page and Word paragraphs/table rows, indexes it with a local Ollama embedding model, and answers questions with source locations. Your documents and index stay on this computer.

## What this version does

- Upload and index text-based PDF books and papers, plus Word `.docx` documents.
- Organize research into Projects, with one topic per project, its own instructions, documents, and local search index.
- Choose which existing projects' documents a new project can use as additional research sources; each project keeps its own documents and index.
- Search the local collection and see the passages selected for an answer.
- Ask a local Ollama chat model to answer from those passages, with source citations.
- Keep uploaded documents and the index in this project’s `data/` directory.
- Choose Light, Dark, or System appearance and configure the Ollama URL and model names in the interface.
- Check local CPU, memory, GPU, and model-folder storage before selecting an answer model.
- Review model and app storage estimates, choose the model-file folder, download compatible multilingual Ollama models, and see live progress and completion notices.
- Review downloaded models and remove a model with a confirmation step when changing to a different size.
- See live indexing progress, the current document/page or passage batch, and an estimated remaining time once the local embedding speed has been measured.

Scanned PDFs, precise Word page-number citations, complex table/equation extraction, OCR, multi-user access, and verified calculation tools are not in this first version. PDF citations use page numbers. Word citations use paragraph numbers or table and row numbers, since page numbering depends on fonts, layout, and the editor. Numeric answers should be checked against the cited source locations. No arbitrary model-generated code is executed.

## Requirements

- Python 3.10 or newer.
- An internet connection for the first-time Ollama and model download. LightWeightRAG installs Ollama automatically after you review and confirm the storage estimate.

You do not need to install Ollama yourself or use a terminal to download models. Open **Model settings**, choose the recommended answer model and the folder for model files, review the Ollama installer and model download/storage estimates, check the confirmation box, then select **Install Ollama and download models**. LightWeightRAG downloads Ollama from its official distribution, installs it for your account, starts its local service, downloads the multilingual Qwen3 answer model and `nomic-embed-text-v2-moe` retrieval model, shows progress, and selects both when setup completes. Windows uses Ollama’s signed per-user installer; macOS and Linux use the official runtime in your user account, without changing system-wide packages.

The app recommends from multilingual Qwen3 0.6B, 1.7B, 4B, 8B, and 14B models based on detected system memory. Qwen3 supports 119 languages and dialects; the retrieval model supports multilingual search across about 100 languages. If the computer is below the guide’s 4 GB minimum, it will not start a model download. Model sizes are approximate and actual runtime needs vary with other open apps, context size, and graphics hardware. See the [Ollama Qwen3 model catalog](https://ollama.com/library/qwen3), [Qwen3 language support](https://qwenlm.github.io/blog/qwen3/), and [Nomic Embed Text v2 model page](https://ollama.com/library/nomic-embed-text-v2-moe) for current details.

In **Model settings**, use **Browse** to choose a folder before downloading. You can also enter a folder path directly. The app checks available space at that location, remembers your choice, and stores new Ollama model files there. Ollama’s model folder is controlled by its `OLLAMA_MODELS` setting; if another Ollama service is already running, its active model folder must match the selected folder. Quit Ollama and refresh the check before changing folders. The **Downloaded models** list shows the models Ollama currently reports; select a delete icon and confirm to remove one and free storage. Documents and the document index are kept separately.

For a terminal-based setup helper, run `python setup_local_llm.py`. It uses the same memory and storage checks and asks before it downloads anything. The one-click Ollama installer is available in the app’s **Model settings**.

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

1. Use **Create project** on the project selection screen to create one project for each research topic. The existing library appears as **General**, so documents from earlier versions remain available.
2. In the creation window, choose any existing projects whose documents should also be available as sources in this new project. Leave them unchecked to keep this project's research context independent. The source projects remain separate, and their documents are not copied.
3. Select a project to open its research workspace, then upload PDF or `.docx` source files. You can also provide optional research instructions when creating the project. Each project keeps its own files and index.
4. Select **Index project sources**. This indexes documents in the open project and any projects it includes. The progress panel shows extraction, passage embedding, and saving. It reports the current PDF page or Word paragraph/table row while reading, then the number of passages processed. The remaining-time estimate starts after the first embedding batch and updates using recent model speed; first-time model loading and different computers can make it vary. A small library may finish before there is enough work for a useful estimate.
5. Ask a question in the research panel. Project instructions for the open project and its included sources guide the answer. Citations such as `[1]` identify the project, passage, and page/paragraph/table-row location.
6. Re-index the project's sources after adding documents to the open or included projects, or after changing the embedding model. The app disables questions until required indexes are ready or marks sources for re-indexing if saved vectors do not match the selected search model. Existing local data can be removed by deleting `data/` while the app is stopped.

The first version works best with PDFs that contain selectable text. If a PDF is a scan, the app will report that it could not extract text; OCR support is planned. Word `.doc` files are not supported yet; save them as `.docx` first. A file over 100 MB is skipped with an on-screen warning; reduce or split it before uploading.

## Privacy and limitations

The application sends prompts and retrieved excerpts only to the Ollama server address configured in **Model settings**. With a local Ollama server, inference stays on your machine. Avoid configuring a remote server for confidential research material. First-time Ollama and model setup needs internet access; once installed and downloaded, local inference does not require a cloud model service.

PDF extraction and semantic search can miss or misread material. Always check the cited source page, especially for numbers, tables, equations, and quotations. An answer without adequate retrieved evidence should be treated as unanswered, not as a claim about the entire book.

## Project layout

- `webapp.py` — local FastAPI server and document/answer API.
- `lightweightrag/model_setup.py` — hardware, storage, model fit, and download-progress helpers.
- `setup_local_llm.py` — cross-platform fallback setup script.
- `web/` — responsive Material 3-inspired browser interface and locally bundled Material Symbols.
- `lightweightrag/` — document extraction, chunking, local index, and Ollama client.
- `data/documents/` — uploaded source documents (created at runtime; ignored by Git).
- `data/index.json` — extracted chunks and embeddings (created at runtime; ignored by Git).
- `data/projects.json` — local project names, instructions, and IDs (created at runtime; ignored by Git).
- `data/projects/<project-id>/documents/` and `data/projects/<project-id>/index.json` — each project’s private local documents and independent index (created at runtime; ignored by Git).
- The legacy **General** project continues using `data/documents/` and `data/index.json`; existing topic folders are carried forward into the Projects list.

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
- **Embedding request failed:** confirm the embedding model supports embeddings and is installed; the default is `nomic-embed-text-v2-moe`.
- **Re-index required:** select **Re-index library** to rebuild passages with the currently selected multilingual embedding model.
- **No text found:** the PDF may be scanned or protected. OCR is not included yet. Word files must be `.docx`; legacy `.doc` files are not supported.
- **Slow indexing or answering:** try a smaller model or fewer pages at a time. Local speed depends on available RAM, CPU, and GPU.
