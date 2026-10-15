from __future__ import annotations

import argparse
import os
from pathlib import Path

from .common import AnalysisError, ROOT, TOOLS, atomic_json, load_json

MODEL_IDS = {"visual": "Qwen/Qwen3-VL-4B-Instruct", "asr": "Systran/faster-whisper-large-v3"}
MANIFEST = TOOLS / "models.json"


def model_manifest() -> dict:
    if not MANIFEST.is_file():
        raise AnalysisError("Модели не загружены. Запустите scripts/download-models.ps1.")
    manifest = load_json(MANIFEST)
    for kind, repo_id in MODEL_IDS.items():
        model = manifest.get(kind, {})
        path = ROOT / model.get("path", "missing-model")
        if model.get("repo_id") != repo_id or not (path / "config.json").is_file():
            raise AnalysisError(f"Не найдена локальная модель {repo_id}: {path}")
    return manifest


def download(refresh: bool = False) -> None:
    os.environ["HF_HOME"] = str(TOOLS / "huggingface")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import HfApi, snapshot_download

    manifest = load_json(MANIFEST) if MANIFEST.is_file() else {}
    api = HfApi()
    for kind, repo_id in MODEL_IDS.items():
        existing = manifest.get(kind, {})
        revision = None if refresh else existing.get("revision")
        if revision is None:
            revision = api.model_info(repo_id).sha
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
    args = parser.parse_args()
    download(args.refresh)
