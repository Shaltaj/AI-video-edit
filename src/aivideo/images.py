"""Local photo decoding and lossless HEIF inputs for browsers and FFmpeg.

Imports remain lazy so the UI server needs no imaging dependencies.
"""
from contextlib import contextmanager
from functools import lru_cache
import importlib.metadata
import os
from pathlib import Path
import uuid

from .common import AnalysisError, atomic_json, load_json, task_key

HEIF_EXTENSIONS = {".heic", ".heif"}


@lru_cache(maxsize=1)
def register_heif():
    try:
        from pillow_heif import register_heif_opener
    except ImportError as exc:
        raise AnalysisError("Не установлен декодер HEIC/HEIF. В диагностике установите «Python и зависимости».") from exc
    register_heif_opener(thumbnails=False)


@contextmanager
def open_photo(path):
    from PIL import Image, ImageOps
    if Path(path).suffix.lower() in HEIF_EXTENSIONS:
        register_heif()
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        try:
            yield image
        finally:
            image.close()


def prepare_photo(media, cache):
    """Decode from the original at full resolution; keep its source identity."""
    settings = {"photo_version": 1, "pillow": importlib.metadata.version("pillow"),
                "pillow_heif": importlib.metadata.version("pillow-heif")}
    key = task_key("photo", media, settings)
    folder = Path(cache) / "photos" / key
    target = folder / "image.png"
    manifest = folder / "manifest.json"
    if target.is_file() and manifest.is_file():
        try:
            value = load_json(manifest)
            if target.stat().st_size == value["size_bytes"]:
                return {**value, "path": str(target), "cache_hit": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f"{uuid.uuid4().hex}.tmp.png"
    source = Path(media.get("path") or Path(media["source_root"]) / media["relative_path"])
    try:
        with open_photo(source) as image:
            image.save(temporary, format="PNG")
            value = {"key": key, "width": image.width, "height": image.height,
                     "size_bytes": temporary.stat().st_size}
        os.replace(temporary, target)
        atomic_json(manifest, value)
    finally:
        temporary.unlink(missing_ok=True)
    return {**value, "path": str(target), "cache_hit": False}


def photo_input(media, cache):
    path = Path(media["path"])
    return prepare_photo(media, cache)["path"] if path.suffix.lower() in HEIF_EXTENSIONS else str(path)
