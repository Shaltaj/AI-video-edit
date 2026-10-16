from __future__ import annotations

import argparse
import os
from pathlib import Path

from .common import AnalysisError, ROOT, TOOLS, atomic_json, load_json

MODEL_IDS = {"visual": "Qwen/Qwen3-VL-4B-Instruct", "asr": "Systran/faster-whisper-large-v3"}
MANIFEST = TOOLS / "models.json"


def missing_files(model: dict, kind: str, root: Path | None = None) -> list[str]:
    root = root or ROOT
    if not isinstance(model, dict) or not isinstance(model.get("path"), str):
        return ["Модель не зарегистрирована"]
    path = root / model.get("path", "missing-model")
    if model.get("repo_id") != MODEL_IDS[kind]:
        return ["Модель не зарегистрирована"]
    required = {"config.json", "tokenizer.json"}
    if kind == "asr":
        required.update({"model.bin", "preprocessor_config.json", "vocabulary.json"})
    else:
        required.update({"preprocessor_config.json", "video_preprocessor_config.json", "tokenizer_config.json", "chat_template.json"})
        index = path / "model.safetensors.index.json"
        if index.exists():
            try:
                required.update(load_json(index)["weight_map"].values())
            except (ValueError, KeyError, TypeError):
                return ["Повреждён индекс весов"]
        else:
            required.add("model.safetensors")
    return [name for name in sorted(required) if not (path / name).is_file() or (path / name).stat().st_size == 0]


def model_manifest() -> dict:
    if not MANIFEST.is_file():
        raise AnalysisError("Модели не загружены. Запустите scripts/download-models.ps1.")
    manifest = load_json(MANIFEST)
    if not isinstance(manifest, dict):
        raise AnalysisError("Некорректный реестр моделей; повторите установку моделей")
    for kind, repo_id in MODEL_IDS.items():
        model = manifest.get(kind, {})
        path = ROOT / model.get("path", "missing-model")
        if missing_files(model, kind):
            raise AnalysisError(f"Не найдена локальная модель {repo_id}: {path}")
    return manifest


def download(refresh: bool = False, kinds=None) -> None:
    os.environ["HF_HOME"] = str(TOOLS / "huggingface")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import HfApi, snapshot_download

    try:
        manifest = load_json(MANIFEST) if MANIFEST.is_file() else {}
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    for kind, repo_id in MODEL_IDS.items():
        if kinds and kind not in kinds:
            continue
        existing = manifest.get(kind, {})
        if not refresh and not missing_files(existing, kind):
            print(f"Ready: {repo_id} (already installed)", flush=True)
            continue
        revision = None if refresh else existing.get("revision")
        if revision is None:
            revision = HfApi().model_info(repo_id).sha
        print(f"Download {kind}: {repo_id}@{revision}", flush=True)
        patterns = ["*.json", "*.txt", "*.model", "*.jinja", "*.tiktoken"]
        patterns += ["*.safetensors"] if kind == "visual" else ["model.bin"]
        path = Path(snapshot_download(repo_id=repo_id, revision=revision,
                                     allow_patterns=patterns, max_workers=4,
                                     cache_dir=TOOLS / "huggingface" / "hub"))
        manifest[kind] = {"repo_id": repo_id, "revision": revision,
                          "path": path.relative_to(ROOT).as_posix()}
        atomic_json(MANIFEST, manifest)
        print(f"Ready: {repo_id}", flush=True)
    print("All model weights are local.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Resolve new revisions explicitly")
    parser.add_argument("--kind", choices=tuple(MODEL_IDS))
    args = parser.parse_args()
    download(args.refresh, [args.kind] if args.kind else None)
