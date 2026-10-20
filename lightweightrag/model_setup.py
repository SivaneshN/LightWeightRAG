"""Local hardware guidance and explicit, progress-reporting Ollama setup."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import math
import os
import platform
import shutil
import subprocess
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLLAMA_URL = "http://localhost:11434"
GIB = 1024**3
GB = 1000**3
MIN_MODEL_RAM_BYTES = 4 * GIB
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_SIZE_BYTES = 274_000_000
MODEL_CHOICES: list[dict[str, Any]] = [
    {"id": "qwen3:0.6b", "label": "Qwen3 0.6B · basic", "size_bytes": 523_000_000, "min_ram_bytes": 4 * GIB, "quality": "Very small; basic answers"},
    {"id": "qwen3:1.7b", "label": "Qwen3 1.7B · light", "size_bytes": 1_400_000_000, "min_ram_bytes": 6 * GIB, "quality": "Lightweight; limited reasoning"},
    {"id": "qwen3:4b", "label": "Qwen3 4B · balanced", "size_bytes": 2_500_000_000, "min_ram_bytes": 8 * GIB, "quality": "Balanced for everyday research questions"},
    {"id": "qwen3:8b", "label": "Qwen3 8B · stronger", "size_bytes": 5_200_000_000, "min_ram_bytes": 12 * GIB, "quality": "Stronger answers and synthesis"},
    {"id": "qwen3:14b", "label": "Qwen3 14B · best fit for high-memory systems", "size_bytes": 9_300_000_000, "min_ram_bytes": 20 * GIB, "quality": "Best quality in this local setup guide"},
]
MODEL_BY_ID = {model["id"]: model for model in MODEL_CHOICES}


def is_local_ollama_url(value: str) -> bool:
    """Only allow setup downloads to an Ollama endpoint on this computer."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.casefold()
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _run_first_line(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2, check=True)
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _total_memory_bytes() -> int | None:
    system = platform.system()
    if system == "Darwin":
        value = _run_first_line(["sysctl", "-n", "hw.memsize"])
        if value and value.isdigit():
            return int(value)
    elif system == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    elif system == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        try:
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except (AttributeError, OSError):
            pass

    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.virtual_memory().total)
    except (ImportError, AttributeError, OSError):
        return None


def _cpu_name() -> str | None:
    if platform.system() == "Darwin":
        name = _run_first_line(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name and name.casefold() not in {"arm", "arm64", "aarch64"}:
            return name
        if platform.machine().casefold() in {"arm64", "aarch64"}:
            return "Apple Silicon"
    name = platform.processor().strip()
    return name or None


def _gpu_info() -> dict[str, Any] | None:
    output = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        output = result.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    if output:
        parts = [part.strip() for part in output.split(",", 1)]
        vram_mb = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return {"name": parts[0], "vram_bytes": vram_mb * 1_000_000 if vram_mb else None}
    if platform.system() == "Darwin" and platform.machine().casefold() in {"arm64", "aarch64"}:
        return {"name": "Apple Silicon integrated graphics (shared memory)", "vram_bytes": None}
    return None


def _model_store_path() -> tuple[Path, bool]:
    custom = os.environ.get("OLLAMA_MODELS", "").strip()
    if custom:
        return Path(custom).expanduser(), True
    return Path.home() / ".ollama" / "models", False


def _available_bytes(path: Path) -> int | None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    try:
        return int(shutil.disk_usage(current).free)
    except OSError:
        return None


def _app_footprint_bytes() -> int:
    total = 0
    try:
        for current, directories, files in os.walk(ROOT, followlinks=False):
            directories[:] = [directory for directory in directories if directory not in {".git", "data", "__pycache__"}]
            for filename in files:
                if filename == ".DS_Store":
                    continue
                try:
                    total += (Path(current) / filename).stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _model_key(name: str) -> str:
    value = name.strip().casefold()
    return value[:-7] if value.endswith(":latest") else value


def _ollama_details(base_url: str) -> dict[str, Any]:
    local = is_local_ollama_url(base_url)
    binary = shutil.which("ollama")
    if not binary and platform.system() == "Windows":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        binary = str(candidate) if candidate.is_file() else None
    if not binary and platform.system() == "Darwin":
        candidate = Path("/Applications/Ollama.app/Contents/Resources/ollama")
        binary = str(candidate) if candidate.is_file() else None

    result: dict[str, Any] = {
        "local_url": local,
        "installed": bool(binary),
        "running": False,
        "models": [],
        "error": None,
    }
    if not local:
        result["error"] = "Model setup is available only for Ollama running on this computer."
        return result
    try:
        response = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=2)
        response.raise_for_status()
        data = response.json()
        result["running"] = True
        result["models"] = [
            {"name": str(model.get("name", "")), "size_bytes": int(model.get("size", 0) or 0)}
            for model in data.get("models", [])
            if model.get("name")
        ]
    except (requests.RequestException, ValueError) as exc:
        result["error"] = "Ollama is installed but not running." if binary else "Ollama is not installed or is not running."
    return result


def _required_ram(model: dict[str, Any]) -> int:
    return int(model["min_ram_bytes"])


def _recommended_model(memory_bytes: int | None) -> str | None:
    if memory_bytes is None or memory_bytes < MIN_MODEL_RAM_BYTES:
        return None
    compatible = [model for model in MODEL_CHOICES if memory_bytes >= _required_ram(model)]
    return compatible[-1]["id"] if compatible else None


def get_system_profile(ollama_url: str = DEFAULT_OLLAMA_URL) -> dict[str, Any]:
    """Return local hardware, storage, Ollama, and per-model fit estimates."""
    memory = _total_memory_bytes()
    store_path, custom_store = _model_store_path()
    free_bytes = _available_bytes(store_path)
    ollama = _ollama_details(ollama_url)
    installed = {_model_key(item["name"]): item for item in ollama["models"]}
    recommended = _recommended_model(memory)
    profiles = []
    for choice in MODEL_CHOICES:
        model_bytes = int(choice["size_bytes"])
        embed_bytes = EMBEDDING_SIZE_BYTES
        missing_models = []
        if _model_key(choice["id"]) not in installed:
            missing_models.append({"name": choice["id"], "size_bytes": model_bytes})
        if _model_key(EMBEDDING_MODEL) not in installed:
            missing_models.append({"name": EMBEDDING_MODEL, "size_bytes": embed_bytes})
        download_bytes = sum(item["size_bytes"] for item in missing_models)
        margin_bytes = max(500_000_000, math.ceil(download_bytes * 0.15)) if download_bytes else 0
        required_free = download_bytes + margin_bytes
        ram_ok = memory is not None and memory >= _required_ram(choice)
        storage_ok = free_bytes is not None and free_bytes >= required_free
        profiles.append({
            **choice,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_size_bytes": embed_bytes,
            "download_bytes": download_bytes,
            "required_free_bytes": required_free,
            "installed": not missing_models,
            "ram_ok": ram_ok,
            "storage_ok": storage_ok,
            "can_setup": ram_ok and storage_ok and ollama["running"] and ollama["local_url"],
            "memory_shortfall_bytes": max(0, _required_ram(choice) - memory) if memory is not None else None,
            "storage_shortfall_bytes": max(0, required_free - free_bytes) if free_bytes is not None else None,
        })

    return {
        "system": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "cpu": _cpu_name(),
            "cpu_threads": os.cpu_count(),
            "memory_bytes": memory,
            "gpu": _gpu_info(),
            "app_footprint_bytes": _app_footprint_bytes(),
        },
        "storage": {
            "free_bytes": free_bytes,
            "custom_location": custom_store,
            "location_label": "Custom Ollama model folder" if custom_store else "Default Ollama model folder",
        },
        "ollama": ollama,
        "recommended_model": recommended,
        "minimum_ram_bytes": MIN_MODEL_RAM_BYTES,
        "models": profiles,
        "guidance": "Memory guidance is a conservative estimate. Other open apps, context length, and graphics hardware affect runtime. PDF/Word library storage is separate and grows as you add documents.",
    }


def _pull_model(base_url: str, model: str, on_progress: Callable[[dict[str, Any]], None]) -> None:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/pull",
            json={"model": model, "stream": True},
            stream=True,
            timeout=(10, None),
        )
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                update = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Ollama returned unreadable setup progress.") from exc
            if update.get("error"):
                raise RuntimeError(str(update["error"]))
            on_progress(update)
    except requests.RequestException as exc:
        detail = getattr(getattr(exc, "response", None), "text", "")
        raise RuntimeError(f"Could not download {model} from Ollama. {detail or exc}") from exc


def pull_model_pair(
    ollama_url: str,
    chat_model: str,
    on_progress: Callable[[str, dict[str, Any]], None],
) -> None:
    """Pull the selected answer model and the app's embedding model in order."""
    if chat_model not in MODEL_BY_ID:
        raise ValueError("Choose one of the recommended local models.")
    for model in (chat_model, EMBEDDING_MODEL):
        _pull_model(ollama_url, model, lambda update, item=model: on_progress(item, update))


class ModelSetupManager:
    """One background pull job with an in-memory progress snapshot for the UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"status": "idle", "progress": 0}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def start(self, ollama_url: str, chat_model: str) -> dict[str, Any]:
        if chat_model not in MODEL_BY_ID:
            raise ValueError("Choose one of the recommended local models.")
        profile = get_system_profile(ollama_url)
        selected = next(item for item in profile["models"] if item["id"] == chat_model)
        if not profile["ollama"]["local_url"]:
            raise ValueError("For privacy, setup only downloads models from Ollama on this computer.")
        if not profile["ollama"]["running"]:
            raise ValueError("Start Ollama, then refresh this system check before downloading.")
        if not selected["ram_ok"]:
            raise ValueError("This computer does not have enough memory for that model. Choose a smaller recommendation.")
        if not selected["storage_ok"]:
            raise ValueError("There is not enough free space for that model and the safety margin. Free space or choose a smaller model.")

        with self._lock:
            if self._state.get("status") == "running":
                raise ValueError("A model setup is already in progress.")
            job_id = uuid.uuid4().hex
            missing = []
            if not any(_model_key(item["name"]) == _model_key(chat_model) for item in profile["ollama"]["models"]):
                missing.append({"name": chat_model, "size_bytes": MODEL_BY_ID[chat_model]["size_bytes"]})
            if not any(_model_key(item["name"]) == _model_key(EMBEDDING_MODEL) for item in profile["ollama"]["models"]):
                missing.append({"name": EMBEDDING_MODEL, "size_bytes": EMBEDDING_SIZE_BYTES})
            total_bytes = sum(item["size_bytes"] for item in missing)
            self._state = {
                "job_id": job_id,
                "status": "running",
                "phase": "Starting model setup",
                "current_model": None,
                "completed_bytes": 0,
                "total_bytes": total_bytes,
                "progress": 0,
                "message": "Preparing the local models…",
                "chat_model": chat_model,
                "embedding_model": EMBEDDING_MODEL,
                "error": None,
            }
        thread = threading.Thread(target=self._run, args=(job_id, ollama_url, chat_model, missing), daemon=True)
        thread.start()
        return self.status()

    def _update(self, job_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            if self._state.get("job_id") == job_id:
                self._state.update(values)

    def _run(self, job_id: str, ollama_url: str, chat_model: str, missing: list[dict[str, Any]]) -> None:
        total_bytes = sum(item["size_bytes"] for item in missing)
        finished_bytes = 0
        try:
            for item in missing:
                model = item["name"]
                model_bytes = item["size_bytes"]
                digest_progress: dict[str, int] = {}
                self._update(job_id, {
                    "phase": f"Downloading {model}",
                    "current_model": model,
                    "message": f"Setting up {model} locally…",
                })

                def report(update: dict[str, Any], model_name: str = model, offset: int = finished_bytes, size: int = model_bytes) -> None:
                    remote_total = int(update.get("total", 0) or 0)
                    remote_completed = int(update.get("completed", 0) or 0)
                    digest = str(update.get("digest") or "")
                    if digest and remote_total:
                        digest_progress[digest] = max(digest_progress.get(digest, 0), min(remote_total, remote_completed))
                        progress_bytes = min(size, sum(digest_progress.values()))
                    else:
                        ratio = min(1.0, remote_completed / remote_total) if remote_total else 0.0
                        progress_bytes = min(size, int(size * ratio))
                    overall = offset + progress_bytes
                    progress = int(overall * 100 / total_bytes) if total_bytes else 100
                    self._update(job_id, {
                        "phase": str(update.get("status") or f"Downloading {model_name}"),
                        "current_model": model_name,
                        "completed_bytes": overall,
                        "total_bytes": total_bytes,
                        "progress": max(0, min(100, progress)),
                        "message": str(update.get("status") or f"Downloading {model_name}…"),
                    })

                _pull_model(ollama_url, model, report)
                finished_bytes += model_bytes
                self._update(job_id, {
                    "completed_bytes": finished_bytes,
                    "total_bytes": total_bytes,
                    "progress": int(finished_bytes * 100 / total_bytes) if total_bytes else 100,
                })
            self._update(job_id, {
                "status": "complete",
                "phase": "Setup complete",
                "current_model": None,
                "completed_bytes": total_bytes,
                "total_bytes": total_bytes,
                "progress": 100,
                "message": "Your local models are ready. The app has selected them for answers and search.",
            })
        except Exception as exc:
            self._update(job_id, {
                "status": "error",
                "phase": "Setup needs attention",
                "error": str(exc),
                "message": str(exc),
            })


setup_manager = ModelSetupManager()
