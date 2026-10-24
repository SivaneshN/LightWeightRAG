"""Build and stage the bundled Python service for the Tauri desktop app."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = ROOT / "desktop" / "src-tauri"
ENTRYPOINT = ROOT / "desktop_backend.py"
RESOURCE_DIR = TAURI_DIR / "resources" / "backend"
APP_NAME = "LightWeightRAG-backend"


def main() -> int:
    if not ENTRYPOINT.is_file():
        raise SystemExit(f"Backend entry point not found: {ENTRYPOINT}")

    with tempfile.TemporaryDirectory(prefix="lwrag-backend-build-") as temporary:
        temp_path = Path(temporary)
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            APP_NAME,
            "--distpath",
            str(temp_path / "dist"),
            "--workpath",
            str(temp_path / "work"),
            "--specpath",
            str(temp_path),
            "--collect-submodules",
            "uvicorn",
            "--add-data",
            f"{ROOT / 'web'}{os.pathsep}web",
            str(ENTRYPOINT),
        ]
        subprocess.run(command, cwd=ROOT, check=True)

        executable_name = APP_NAME + (".exe" if os.name == "nt" else "")
        built_executable = temp_path / "dist" / executable_name
        if not built_executable.is_file():
            raise SystemExit(f"PyInstaller did not produce the expected backend: {built_executable}")

        RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
        for old_executable in RESOURCE_DIR.glob(APP_NAME + "*"):
            if old_executable.is_file():
                old_executable.unlink()
        shutil.copy2(built_executable, RESOURCE_DIR / executable_name)

    print(f"Staged desktop backend at {RESOURCE_DIR / executable_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
