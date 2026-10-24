"""Interactive cross-platform fallback for downloading the recommended local models.

Most students should use Model settings in the app. This script uses the same
hardware and storage checks and can also be started from a terminal when needed.
"""

from __future__ import annotations

from lightweightrag.model_setup import DEFAULT_OLLAMA_URL, EMBEDDING_MODEL, get_system_profile, pull_model_pair


def format_size(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1000 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} {unit}"
        amount /= 1000
    return f"{amount:.1f} TB"


def main() -> int:
    print("LightWeightRAG · local model setup\n")
    profile = get_system_profile(DEFAULT_OLLAMA_URL)
    system = profile["system"]
    storage = profile["storage"]
    ollama = profile["ollama"]
    print(f"Computer: {system['os']} {system['architecture']} · {system['cpu_threads'] or 'unknown'} CPU threads")
    print(f"Memory: {format_size(system['memory_bytes'])}")
    print(f"LightWeightRAG app files: {format_size(system['app_footprint_bytes'])} (books and model files are separate)")
    print(f"Available in {storage['location_label'].lower()}: {format_size(storage['free_bytes'])}")

    if not ollama["running"]:
        if ollama["installed"]:
            print("\nOllama is installed but is not running. Open Ollama, then run this script again.")
        else:
            print("\nOllama is not installed. Install it from the official page, then run this script again:")
            print("https://ollama.com/download")
        return 1

    if system["memory_bytes"] is None or system["memory_bytes"] < profile["minimum_ram_bytes"]:
        print("\nThis computer has less than 4 GB of RAM or its memory could not be detected.")
        print("The app will not download a model that is unlikely to run reliably.")
        return 1

    compatible = [model for model in profile["models"] if model["ram_ok"]]
    default_id = profile["recommended_model"]
    print("\nModels that fit the detected memory:")
    for index, item in enumerate(compatible, start=1):
        recommended = " · recommended" if item["id"] == default_id else ""
        print(f"  {index}. {item['id']} + {item['embedding_model']} · downloads about {format_size(item['download_bytes'])}{recommended}")
    choice = input("Choose a number, or press Enter for the recommendation: ").strip()
    if not choice:
        chat_model = default_id
    else:
        try:
            chat_model = compatible[int(choice) - 1]["id"]
        except (ValueError, IndexError):
            print("\nThat choice was not recognized. No files were downloaded.")
            return 1
    selected = next(item for item in profile["models"] if item["id"] == chat_model)

    print(f"\nModel files: about {format_size(selected['download_bytes'])}")
    print(f"Recommended free space, including download headroom: {format_size(selected['required_free_bytes'])}")
    print(f"Free space now: {format_size(storage['free_bytes'])}")
    if not selected["storage_ok"]:
        shortfall = selected["storage_shortfall_bytes"]
        print(f"\nThere is not enough space. Free about {format_size(shortfall)} or choose a smaller model.")
        if default_id and chat_model == default_id:
            print("Your RAM supports the recommended model; freeing disk space is preferable to upgrading RAM.")
        return 1

    print(f"\nThe app will download the multilingual answer model and {EMBEDDING_MODEL} search model to Ollama on this computer.")
    if input("Continue? [y/N] ").strip().casefold() not in {"y", "yes"}:
        print("\nNo files were downloaded.")
        return 0

    last_progress: dict[str, int] = {}

    def show_progress(model: str, update: dict) -> None:
        total = int(update.get("total", 0) or 0)
        completed = int(update.get("completed", 0) or 0)
        percent = int(completed * 100 / total) if total else 0
        status = str(update.get("status") or "Preparing")
        if status != last_progress.get(model) or percent >= last_progress.get(model + "_percent", -5) + 5:
            suffix = f" · {percent}%" if total else ""
            print(f"{model}: {status}{suffix}")
            last_progress[model] = status
            last_progress[model + "_percent"] = percent

    try:
        pull_model_pair(DEFAULT_OLLAMA_URL, chat_model, show_progress)
    except Exception as exc:
        print(f"\nSetup did not finish: {exc}")
        return 1
    print("\nSetup complete. Open Model settings in LightWeightRAG and select these models if needed.")
    print(f"Answer model: {chat_model}\nSearch model: {EMBEDDING_MODEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
