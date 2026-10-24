# Desktop installer builds

LightWeightRAG desktop installers bundle the Python service with a Tauri window. The service listens only on the local computer, and research files, indexes, settings, and logs are kept in the user's per-user app data folder. Ollama and language models are installed separately through the app's first-run setup.

## Download a preview installer

The GitHub Actions workflow is `.github/workflows/desktop-build.yml`. After the workflow is available in the GitHub repository:

1. Open the repository's **Actions** tab and select **Build desktop installers (unsigned preview)**.
2. Select **Run workflow** and choose the branch to build.
3. When the four builds finish, open the run and download the artifact for the recipient's system:
   - `LightWeightRAG-windows-x64` contains a Windows installer (`.exe`).
   - `LightWeightRAG-macos-arm64` contains a macOS disk image (`.dmg`) for Apple Silicon.
   - `LightWeightRAG-macos-x64` contains a macOS disk image (`.dmg`) for Intel Macs.
   - `LightWeightRAG-linux-ubuntu-x64` contains an Ubuntu/Debian package (`.deb`).

These are unsigned preview builds, retained by Actions for 14 days. They are useful for internal review, but operating systems may show trust warnings. Before distributing installers to non-technical users, set up release signing: Developer ID signing and notarization for macOS, and a trusted code-signing certificate or Microsoft Store distribution for Windows. The Linux package targets Ubuntu and Debian.

The workflow also runs for a `v*` tag. It uploads the same preview artifacts to that workflow run; it does not publish a GitHub Release.

## Build on a developer computer

Build on the target operating system and CPU architecture. PyInstaller creates a native Python service, so a Windows build must run on Windows, and macOS Intel and Apple Silicon builds must run on matching Macs. The automated workflow already provides this build matrix.

Install Python 3.11, Rust, and the platform's Tauri prerequisites. On Linux, the workflow installs the needed WebKitGTK and application-indicator development packages on Ubuntu 24.04. Then, from the repository root, create a Python environment and stage the backend:

### macOS and Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
(cd packaging && python -m pip install -r requirements-desktop.txt)
python packaging/build_backend.py
cargo install tauri-cli --locked --version '^2.0.0'
cargo tauri icon desktop-ui/app-icon.png --output desktop/src-tauri/icons
```

Build the Mac app bundle, then create a drag-and-drop disk image. The helper uses `hdiutil` directly and does not need Finder automation permission:

```sh
cd desktop/src-tauri
cargo tauri build --bundles app --no-sign
cd ../..
bash packaging/build_macos_dmg.sh
```

On Ubuntu/Debian, build from `desktop/src-tauri` with `cargo tauri build --bundles deb --no-sign`; on Windows, use `cargo tauri build --bundles nsis --no-sign`. Build outputs are placed under `desktop/src-tauri/target/release/bundle/`.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
Push-Location packaging
python -m pip install -r requirements-desktop.txt
Pop-Location
python packaging/build_backend.py
cargo install tauri-cli --locked --version "^2.0.0"
cargo tauri icon desktop-ui/app-icon.png --output desktop/src-tauri/icons
Set-Location desktop/src-tauri
cargo tauri build --bundles nsis --no-sign
```

For distribution, follow the signing instructions for [Tauri](https://tauri.app/distribute/sign/). The workflow intentionally uses `--no-sign` and should not be treated as a finished public release pipeline until signing credentials and the release process are configured.
