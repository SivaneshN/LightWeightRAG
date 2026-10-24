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
import tarfile
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from copy import deepcopy
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

import requests


ROOT = Path(__file__).resolve().parents[1]
MODEL_DATA_DIR = Path(os.environ.get("LIGHTWEIGHTRAG_DATA_DIR", ROOT / "data")).expanduser()
MODEL_DIRECTORY_CONFIG = MODEL_DATA_DIR / "ollama_model_directory.json"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
GIB = 1024**3
GB = 1000**3
MIN_MODEL_RAM_BYTES = 4 * GIB
EMBEDDING_MODEL = "nomic-embed-text-v2-moe"
EMBEDDING_SIZE_BYTES = 958_000_000
MODEL_CHOICES: list[dict[str, Any]] = [
    {"id": "qwen3:0.6b", "label": "Qwen3 0.6B · multilingual basic", "size_bytes": 523_000_000, "min_ram_bytes": 4 * GIB, "quality": "Very small; multilingual basic answers"},
    {"id": "qwen3:1.7b", "label": "Qwen3 1.7B · multilingual light", "size_bytes": 1_400_000_000, "min_ram_bytes": 6 * GIB, "quality": "Lightweight multilingual answers; limited reasoning"},
    {"id": "qwen3:4b", "label": "Qwen3 4B · multilingual balanced", "size_bytes": 2_500_000_000, "min_ram_bytes": 8 * GIB, "quality": "Balanced multilingual answers for everyday research questions"},
    {"id": "qwen3:8b", "label": "Qwen3 8B · multilingual stronger", "size_bytes": 5_200_000_000, "min_ram_bytes": 12 * GIB, "quality": "Stronger multilingual answers and synthesis"},
    {"id": "qwen3:14b", "label": "Qwen3 14B · multilingual, high memory", "size_bytes": 9_300_000_000, "min_ram_bytes": 20 * GIB, "quality": "Best quality in this multilingual local setup guide"},
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


def _model_store_path(selected_path: str | None = None) -> tuple[Path, bool]:
    if selected_path and selected_path.strip():
        return Path(selected_path).expanduser().resolve(strict=False), True
    custom = os.environ.get("OLLAMA_MODELS", "").strip()
    if custom:
        return Path(custom).expanduser().resolve(strict=False), True
    return (Path.home() / ".ollama" / "models").resolve(strict=False), False


def validate_model_directory(value: str | None) -> Path:
    """Normalize a user-selected model directory and check that it is usable."""
    selected, _ = _model_store_path(value)
    if not selected.is_absolute() or selected == Path(selected.anchor):
        raise ValueError("Choose a folder inside a drive, not the drive or system root.")
    if selected.exists() and not selected.is_dir():
        raise ValueError("The selected model location is a file. Choose a folder instead.")
    available = selected
    while not available.exists() and available != available.parent:
        available = available.parent
    if not available.is_dir() or not os.access(available, os.W_OK | os.X_OK):
        raise ValueError("LightWeightRAG cannot write to that folder. Choose a folder you can access.")
    return selected


def _remember_model_directory(model_directory: Path) -> None:
    """Keep the managed Ollama path available after the app is restarted."""
    MODEL_DIRECTORY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = MODEL_DIRECTORY_CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps({"path": str(model_directory)}), encoding="utf-8")
    temporary.replace(MODEL_DIRECTORY_CONFIG)


def _remembered_model_directory() -> Path | None:
    try:
        path = json.loads(MODEL_DIRECTORY_CONFIG.read_text(encoding="utf-8")).get("path")
        return validate_model_directory(path) if path else None
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return None


def choose_model_directory() -> str | None:
    """Open the operating system's folder chooser when a desktop picker is available."""
    system = platform.system()
    if system == "Darwin":
        script = 'POSIX path of (choose folder with prompt "Choose a folder for Ollama model files")'
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode:
            if "-128" in result.stderr:
                return None
            raise RuntimeError(result.stderr.strip() or "The macOS folder chooser could not open.")
        return result.stdout.strip().rstrip("/") or "/"
    if system == "Windows":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("The folder chooser is unavailable. You can type a folder path instead.")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dialog.Description = 'Choose a folder for Ollama model files'; "
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }"
        )
        result = subprocess.run([powershell, "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "The Windows folder chooser could not open.")
        return result.stdout.strip() or None

    for picker, args in (("zenity", ["--file-selection", "--directory", "--title=Choose a folder for Ollama model files"]),
                         ("kdialog", ["--getexistingdirectory", str(Path.home())])):
        executable = shutil.which(picker)
        if executable:
            result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=120)
            if result.returncode:
                return None
            return result.stdout.strip() or None
    try:
        import tkinter
        from tkinter import filedialog

        window = tkinter.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        selected = filedialog.askdirectory(parent=window, title="Choose a folder for Ollama model files")
        window.destroy()
        return selected or None
    except Exception as exc:
        raise RuntimeError("The folder chooser is unavailable. Type a folder path instead.") from exc


def _normalized_architecture() -> str:
    value = platform.machine().casefold()
    if value in {"x86_64", "amd64", "x64"}:
        return "amd64"
    if value in {"arm64", "aarch64", "arm64e"}:
        return "arm64"
    return value


def _install_target_path() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Programs" / "Ollama"
    if system == "Darwin":
        return Path.home() / "Applications"
    return Path.home() / ".local" / "share" / "lightweightrag" / "ollama"


def _temporary_download_path() -> Path:
    return Path(tempfile.gettempdir())


def _installer_spec() -> dict[str, Any] | None:
    system = platform.system()
    arch = _normalized_architecture()
    if system == "Darwin" and arch in {"amd64", "arm64"}:
        return {
            "url": "https://ollama.com/download/Ollama-darwin.zip",
            "download_bytes": 230_000_000,
            "installed_bytes": 600_000_000,
            "kind": "macos-zip",
        }
    if system == "Linux" and arch in {"amd64", "arm64"}:
        return {
            "url": f"https://ollama.com/download/ollama-linux-{arch}.tar.zst",
            "download_bytes": 1_500_000_000,
            "installed_bytes": 5 * GIB,
            "kind": "linux-archive",
        }
    if system == "Windows" and arch == "amd64":
        return {
            "url": "https://ollama.com/download/OllamaSetup.exe",
            "download_bytes": 1_600_000_000,
            "installed_bytes": 4 * GIB,
            "kind": "windows-signed-installer",
        }
    return None


@lru_cache(maxsize=8)
def _remote_file_size(url: str, fallback: int) -> int:
    try:
        response = requests.head(url, allow_redirects=True, timeout=(2, 4))
        response.raise_for_status()
        value = int(response.headers.get("Content-Length", "0"))
        return value if value > 0 else fallback
    except (requests.RequestException, TypeError, ValueError):
        return fallback


def _find_ollama_binary() -> str | None:
    candidates: list[Path] = []
    located = shutil.which("ollama")
    if located:
        candidates.append(Path(located))
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        candidates.extend((base / "Programs" / "Ollama" / "ollama.exe", base / "Ollama" / "ollama.exe"))
    elif system == "Darwin":
        candidates.extend((
            Path.home() / "Applications" / "Ollama.app" / "Contents" / "Resources" / "ollama",
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        ))
    elif system == "Linux":
        candidates.append(_install_target_path() / "bin" / "ollama")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _filesystem_details(path: Path) -> tuple[int | None, int | None]:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    try:
        return int(current.stat().st_dev), int(shutil.disk_usage(current).free)
    except OSError:
        return None, None


def _disk_budget(requirements: list[tuple[Path, int]]) -> tuple[bool, int | None]:
    """Check combined peak space for paths that may share one filesystem."""
    groups: dict[int, dict[str, int]] = {}
    unknown = 0
    for path, required in requirements:
        if required <= 0:
            continue
        device, free = _filesystem_details(path)
        if device is None or free is None:
            unknown += required
            continue
        group = groups.setdefault(device, {"required": 0, "free": free})
        group["required"] += required
        group["free"] = min(group["free"], free)
    shortfall = max((item["required"] - item["free"] for item in groups.values()), default=0)
    if unknown:
        return False, None
    return shortfall <= 0, max(0, shortfall)


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
    binary = _find_ollama_binary()
    install_spec = _installer_spec()
    install_download_bytes = 0
    install_storage_bytes = 0
    if not binary and install_spec:
        install_download_bytes = _remote_file_size(install_spec["url"], install_spec["download_bytes"])
        install_storage_bytes = int(install_spec["installed_bytes"])

    result: dict[str, Any] = {
        "local_url": local,
        "installed": bool(binary),
        "running": False,
        "install_supported": bool(install_spec),
        "installation_download_bytes": install_download_bytes,
        "installation_storage_bytes": install_storage_bytes,
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


def get_system_profile(
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model_directory: str | None = None,
) -> dict[str, Any]:
    """Return local hardware, storage, Ollama, and per-model fit estimates."""
    memory = _total_memory_bytes()
    if not model_directory and not os.environ.get("OLLAMA_MODELS", "").strip():
        remembered = _remembered_model_directory()
        if remembered:
            model_directory = str(remembered)
    store_path, custom_store = _model_store_path(model_directory)
    store_path = validate_model_directory(str(store_path))
    free_bytes = _available_bytes(store_path)
    install_free_bytes = _available_bytes(_install_target_path())
    temp_free_bytes = _available_bytes(_temporary_download_path())
    ollama = _ollama_details(ollama_url)
    active_store = _active_ollama_model_directory() if ollama["running"] else None
    ollama["active_model_directory"] = str(active_store) if active_store else None
    ollama["model_directory_compatible"] = not ollama["running"] or active_store == store_path
    installed = {_model_key(item["name"]): item for item in ollama["models"]}
    recommended = _recommended_model(memory)
    install_needed = not ollama["installed"] and not ollama["running"]
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
        storage_requirements = [(store_path, required_free)]
        if install_needed:
            storage_requirements.extend((
                (_install_target_path(), int(ollama["installation_storage_bytes"])),
                (_temporary_download_path(), int(ollama["installation_download_bytes"])),
            ))
        setup_storage_ok, setup_storage_shortfall = _disk_budget(storage_requirements)
        profiles.append({
            **choice,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_size_bytes": embed_bytes,
            "download_bytes": download_bytes,
            "setup_download_bytes": download_bytes + (int(ollama["installation_download_bytes"]) if install_needed else 0),
            "required_free_bytes": required_free,
            "setup_storage_ok": setup_storage_ok,
            "setup_storage_shortfall_bytes": setup_storage_shortfall,
            "installed": not missing_models,
            "ram_ok": ram_ok,
            "storage_ok": storage_ok,
            "can_setup": ram_ok and storage_ok and setup_storage_ok and ollama["local_url"] and ollama["model_directory_compatible"] and (ollama["running"] or ollama["installed"] or ollama["install_supported"]),
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
            "installation_free_bytes": install_free_bytes,
            "installer_temp_free_bytes": temp_free_bytes,
            "model_directory": str(store_path),
            "default_model_directory": str(_model_store_path()[0]),
            "custom_location": custom_store,
            "location_label": "Selected Ollama model folder" if custom_store else "Default Ollama model folder",
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


def _download_official_installer(
    url: str,
    destination: Path,
    expected_bytes: int,
    on_progress: Callable[[int, int], None],
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        with requests.get(url, stream=True, allow_redirects=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            try:
                total = int(response.headers.get("Content-Length", "0")) or expected_bytes
            except ValueError:
                total = expected_bytes
            with destination.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    downloaded += len(chunk)
                    on_progress(downloaded, total)
    except (OSError, requests.RequestException) as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Could not download the official Ollama installer. {exc}") from exc
    if downloaded == 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError("The Ollama download was empty. Check the internet connection and try again.")
    return downloaded


def _install_ollama_windows(installer: Path, model_directory: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Windows PowerShell was not found, so Ollama could not be installed automatically.")
    # Match Ollama's own installer check: require a valid Authenticode
    # signature from Ollama Inc. before running the signed per-user installer.
    command = r"""
$ErrorActionPreference = 'Stop'
$installer = $env:LWRAG_OLLAMA_INSTALLER
$signature = Get-AuthenticodeSignature -FilePath $installer
$subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
if ($signature.Status -ne 'Valid' -or $subject -notmatch '(^|, )O=Ollama Inc\.(,|$)') { throw 'Ollama installer signature verification failed.' }
$process = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES' -Wait -PassThru
if ($process.ExitCode -ne 0) { exit $process.ExitCode }
"""
    environment = os.environ.copy()
    environment["LWRAG_OLLAMA_INSTALLER"] = str(installer)
    environment["OLLAMA_MODELS"] = str(model_directory)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        timeout=900,
        env=environment,
        creationflags=flags,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"Ollama's signed installer exited with code {result.returncode}.")


def _install_ollama_macos(archive: Path) -> None:
    app_home = Path.home() / "Applications"
    target = app_home / "Ollama.app"
    if target.exists():
        raise RuntimeError("An Ollama app folder already exists but could not be started. Remove or repair it, then try again.")
    app_home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lwrag-ollama-") as temp_name:
        temp = Path(temp_name)
        extracted = temp / "extracted"
        extracted.mkdir()
        ditto = "/usr/bin/ditto"
        codesign = "/usr/bin/codesign"
        spctl = "/usr/sbin/spctl"
        if not Path(ditto).is_file() or not Path(codesign).is_file() or not Path(spctl).is_file():
            raise RuntimeError("macOS archive and security tools were not found. Ollama was not installed.")
        result = subprocess.run([ditto, "-x", "-k", str(archive), str(extracted)], capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "The official macOS Ollama archive could not be unpacked.")
        app = extracted / "Ollama.app"
        if not app.is_dir():
            raise RuntimeError("The downloaded Ollama archive did not contain Ollama.app.")
        for command in ([codesign, "--verify", "--deep", "--strict", str(app)], [spctl, "--assess", "--type", "execute", str(app)]):
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError("macOS could not verify Ollama's app signature. The app was not installed.")
        shutil.move(str(app), str(target))


def _safe_extract_linux_archive(archive: Path, destination: Path) -> None:
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError("The app's archive support is missing. Reinstall the app dependencies and try again.") from exc

    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    members_seen = 0
    total_unpacked = 0
    try:
        with archive.open("rb") as compressed:
            reader = zstandard.ZstdDecompressor().stream_reader(compressed)
            with tarfile.open(fileobj=reader, mode="r|") as package:
                for member in package:
                    members_seen += 1
                    if members_seen > 200_000:
                        raise RuntimeError("The Ollama archive contains too many files.")
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or ".." in name.parts:
                        raise RuntimeError("The Ollama archive contains an unsafe file path.")
                    total_unpacked += max(0, member.size)
                    if total_unpacked > 20 * GIB:
                        raise RuntimeError("The Ollama archive expands beyond the safe installation limit.")
                    target = (root / Path(*name.parts)).resolve()
                    if target != root and root not in target.parents:
                        raise RuntimeError("The Ollama archive contains an unsafe file path.")
                    if member.issym():
                        link_target = (target.parent / member.linkname).resolve()
                        if link_target != root and root not in link_target.parents:
                            raise RuntimeError("The Ollama archive contains an unsafe symbolic link.")
                    elif member.islnk():
                        link_target = (root / member.linkname).resolve()
                        if link_target != root and root not in link_target.parents:
                            raise RuntimeError("The Ollama archive contains an unsafe hard link.")
                    package.extract(member, path=root)
    except (OSError, tarfile.TarError, zstandard.ZstdError) as exc:
        raise RuntimeError(f"The official Linux Ollama archive could not be unpacked. {exc}") from exc
    finally:
        try:
            reader.close()  # type: ignore[possibly-undefined]
        except (NameError, OSError):
            pass


def _install_ollama_linux(archive: Path) -> None:
    destination = _install_target_path()
    binary = destination / "bin" / "ollama"
    if binary.exists():
        raise RuntimeError("An Ollama runtime folder already exists but could not be started. Remove or repair it, then try again.")
    _safe_extract_linux_archive(archive, destination)
    if not binary.is_file():
        raise RuntimeError("The official Linux archive did not contain the Ollama runtime in the expected location.")
    binary.chmod(binary.stat().st_mode | 0o111)


def install_ollama(on_download: Callable[[int, int], None], model_directory: Path) -> int:
    """Install Ollama for the current user from its official distribution."""
    spec = _installer_spec()
    if not spec:
        raise RuntimeError(f"One-click Ollama installation is not available for {platform.system()} {platform.machine()}.")
    with tempfile.TemporaryDirectory(prefix="lwrag-ollama-download-") as temp_name:
        installer = Path(temp_name) / ("ollama-installer" + (".exe" if spec["kind"] == "windows-signed-installer" else ".archive"))
        actual_bytes = _download_official_installer(spec["url"], installer, int(spec["download_bytes"]), on_download)
        if spec["kind"] == "windows-signed-installer":
            _install_ollama_windows(installer, model_directory)
        elif spec["kind"] == "macos-zip":
            _install_ollama_macos(installer)
        elif spec["kind"] == "linux-archive":
            _install_ollama_linux(installer)
        else:
            raise RuntimeError("The Ollama installer for this computer is not supported.")
        return actual_bytes


_OLLAMA_CHILD: subprocess.Popen[bytes] | None = None
_OLLAMA_START_LOCK = threading.Lock()
_OLLAMA_ACTIVE_MODEL_DIRECTORY: Path | None = None


def _active_ollama_model_directory() -> Path:
    if _OLLAMA_CHILD is not None and _OLLAMA_CHILD.poll() is None and _OLLAMA_ACTIVE_MODEL_DIRECTORY is not None:
        return _OLLAMA_ACTIVE_MODEL_DIRECTORY
    return _remembered_model_directory() or _model_store_path()[0]


def _ollama_is_running(base_url: str) -> bool:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=1.5)
        response.raise_for_status()
        response.json()
        return True
    except (requests.RequestException, ValueError):
        return False


def _ensure_ollama_running(base_url: str, on_phase: Callable[[str], None], model_directory: Path) -> None:
    global _OLLAMA_CHILD, _OLLAMA_ACTIVE_MODEL_DIRECTORY
    model_directory = validate_model_directory(str(model_directory))
    on_phase("Starting Ollama on this computer")
    with _OLLAMA_START_LOCK:
        if _ollama_is_running(base_url):
            child_managed = _OLLAMA_CHILD is not None and _OLLAMA_CHILD.poll() is None
            active_directory = _active_ollama_model_directory()
            if active_directory != model_directory:
                if not child_managed:
                    raise RuntimeError("Ollama is already running with another model folder. Quit Ollama, refresh this screen, then try again.")
                _OLLAMA_CHILD.terminate()
                try:
                    _OLLAMA_CHILD.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _OLLAMA_CHILD.kill()
                    _OLLAMA_CHILD.wait(timeout=5)
                _OLLAMA_CHILD = None
                _OLLAMA_ACTIVE_MODEL_DIRECTORY = None
            else:
                _remember_model_directory(model_directory)
                return
        binary = _find_ollama_binary()
        if not binary:
            raise RuntimeError("Ollama was installed, but its local runtime could not be found. Restart LightWeightRAG and try again.")
        if not _ollama_is_running(base_url):
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": {**os.environ, "OLLAMA_MODELS": str(model_directory)},
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            try:
                model_directory.mkdir(parents=True, exist_ok=True)
                _OLLAMA_CHILD = subprocess.Popen([binary, "serve"], **kwargs)
                _OLLAMA_ACTIVE_MODEL_DIRECTORY = model_directory
                _remember_model_directory(model_directory)
            except OSError as exc:
                raise RuntimeError(f"Ollama could not start on this computer. {exc}") from exc
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _ollama_is_running(base_url):
            return
        if _OLLAMA_CHILD and _OLLAMA_CHILD.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError("Ollama did not become ready. Restart the app and try setup again.")


class ModelSetupManager:
    """One background pull job with an in-memory progress snapshot for the UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {"status": "idle", "progress": 0}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def start(self, ollama_url: str, chat_model: str, model_directory: str | None = None) -> dict[str, Any]:
        if chat_model not in MODEL_BY_ID:
            raise ValueError("Choose one of the recommended local models.")
        if not model_directory and not os.environ.get("OLLAMA_MODELS", "").strip():
            model_directory = str(_remembered_model_directory() or _model_store_path()[0])
        selected_directory = validate_model_directory(model_directory)
        profile = get_system_profile(ollama_url, str(selected_directory))
        selected = next(item for item in profile["models"] if item["id"] == chat_model)
        ollama = profile["ollama"]
        if not ollama["local_url"]:
            raise ValueError("For privacy, setup only downloads models from Ollama on this computer.")
        if not ollama["model_directory_compatible"]:
            raise ValueError("Ollama is already running with another model folder. Quit Ollama, refresh this screen, then try again.")
        if not selected["ram_ok"]:
            raise ValueError("This computer does not have enough memory for that model. Choose a smaller recommendation.")
        if not (ollama["running"] or ollama["installed"] or ollama["install_supported"]):
            raise ValueError("One-click Ollama installation is not supported on this computer.")
        if not selected["setup_storage_ok"]:
            shortfall = selected["setup_storage_shortfall_bytes"]
            advice = f" Free about {format_bytes(shortfall)} first." if shortfall else " Check the available storage and try again."
            raise ValueError("There is not enough storage for Ollama and the selected models." + advice)

        with self._lock:
            if self._state.get("status") == "running":
                raise ValueError("A model setup is already in progress.")
            job_id = uuid.uuid4().hex
            install_needed = not ollama["running"] and not ollama["installed"]
            installer_bytes = int(ollama["installation_download_bytes"]) if install_needed else 0
            missing = []
            if not any(_model_key(item["name"]) == _model_key(chat_model) for item in ollama["models"]):
                missing.append({"name": chat_model, "size_bytes": MODEL_BY_ID[chat_model]["size_bytes"]})
            if not any(_model_key(item["name"]) == _model_key(EMBEDDING_MODEL) for item in ollama["models"]):
                missing.append({"name": EMBEDDING_MODEL, "size_bytes": EMBEDDING_SIZE_BYTES})
            total_bytes = installer_bytes + sum(item["size_bytes"] for item in missing)
            self._state = {
                "job_id": job_id,
                "status": "running",
                "phase": "Preparing local setup",
                "current_model": None,
                "completed_bytes": 0,
                "total_bytes": total_bytes,
                "progress": 0,
                "message": "Preparing Ollama and the selected models on this computer…",
                "chat_model": chat_model,
                "embedding_model": EMBEDDING_MODEL,
                "model_directory": str(selected_directory),
                "ollama_install_needed": install_needed,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run,
            args=(job_id, ollama_url, chat_model, install_needed, installer_bytes, selected_directory),
            daemon=True,
        )
        thread.start()
        return self.status()

    def _update(self, job_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            if self._state.get("job_id") == job_id:
                self._state.update(values)

    def _run(
        self,
        job_id: str,
        ollama_url: str,
        chat_model: str,
        install_needed: bool,
        installer_bytes: int,
        model_directory: Path,
    ) -> None:
        installed_bytes = 0
        try:
            if install_needed:
                initial_total = int(self.status().get("total_bytes", 0))
                estimated_model_bytes = max(0, initial_total - installer_bytes)

                def install_progress(completed: int, total: int) -> None:
                    estimated_total = max(installer_bytes, total)
                    overall_total = estimated_total + estimated_model_bytes
                    progress = int(completed * 100 / overall_total) if overall_total else 0
                    self._update(job_id, {
                        "phase": "Downloading Ollama",
                        "current_model": None,
                        "completed_bytes": completed,
                        "total_bytes": overall_total,
                        "progress": max(0, min(99, progress)),
                        "message": "Downloading the official Ollama installer…",
                    })

                self._update(job_id, {
                    "phase": "Downloading Ollama",
                    "message": "Downloading Ollama from its official distribution…",
                })
                installed_bytes = install_ollama(install_progress, model_directory)
                self._update(job_id, {
                    "phase": "Installing Ollama",
                    "completed_bytes": installed_bytes,
                    "total_bytes": installed_bytes + estimated_model_bytes,
                    "message": "Installing Ollama for your account…",
                })

            _ensure_ollama_running(
                ollama_url,
                lambda phase: self._update(job_id, {"phase": phase, "message": phase + "…"}),
                model_directory,
            )
            profile = get_system_profile(ollama_url, str(model_directory))
            selected = next(item for item in profile["models"] if item["id"] == chat_model)
            if not selected["ram_ok"]:
                raise RuntimeError("The computer no longer has enough memory for the selected model.")
            if not selected["storage_ok"]:
                raise RuntimeError("There is not enough free space for the models. Free storage or choose a smaller model.")

            installed_models = profile["ollama"]["models"]
            missing = []
            if not any(_model_key(item["name"]) == _model_key(chat_model) for item in installed_models):
                missing.append({"name": chat_model, "size_bytes": MODEL_BY_ID[chat_model]["size_bytes"]})
            if not any(_model_key(item["name"]) == _model_key(EMBEDDING_MODEL) for item in installed_models):
                missing.append({"name": EMBEDDING_MODEL, "size_bytes": EMBEDDING_SIZE_BYTES})
            model_bytes_total = sum(item["size_bytes"] for item in missing)
            total_bytes = installed_bytes + model_bytes_total
            self._update(job_id, {
                "total_bytes": total_bytes,
                "phase": "Preparing local models",
                "message": "Ollama is ready. Preparing the selected research models…",
            })
            finished_bytes = installed_bytes
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
                "model_directory": str(model_directory),
            })
        except Exception as exc:
            self._update(job_id, {
                "status": "error",
                "phase": "Setup needs attention",
                "error": str(exc),
                "message": str(exc),
            })

    def delete_model(self, ollama_url: str, model_name: str) -> None:
        if not is_local_ollama_url(ollama_url):
            raise ValueError("For privacy, downloaded models can be managed only on this computer.")
        with self._lock:
            if self._state.get("status") == "running":
                raise ValueError("Wait for the current model setup to finish before deleting a model.")
        profile = _ollama_details(ollama_url)
        if not profile["running"]:
            raise ValueError("Start Ollama before managing downloaded models.")
        match = next((item for item in profile["models"] if item["name"] == model_name), None)
        if not match:
            raise ValueError("That model is no longer in the downloaded model list. Refresh and try again.")
        try:
            response = requests.delete(
                f"{ollama_url.rstrip('/')}/api/delete",
                json={"model": match["name"]},
                timeout=120,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = getattr(getattr(exc, "response", None), "text", "")
            raise RuntimeError(f"Could not remove {match['name']}. {detail or exc}") from exc


setup_manager = ModelSetupManager()


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} {unit}"
        amount /= 1000
    return f"{amount:.1f} TB"
