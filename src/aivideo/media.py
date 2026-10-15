from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from PIL import Image, ImageOps

from .common import (AnalysisError, SCHEMA_VERSION, atomic_json, command, executable,
                     load_json, now, task_key, tool_version)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}


def probe(path: Path) -> dict:
    result = command([executable("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                      "-of", "json", path], timeout=60)
    return json.loads(result.stdout)


def scan(source: Path) -> dict:
    source = source.resolve()
    if not source.is_dir():
        raise AnalysisError(f"Папка исходников недоступна: {source}")
    media, errors = [], []
    for path in sorted(source.rglob("*"), key=lambda p: p.relative_to(source).as_posix().casefold()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
            continue
        relative = path.relative_to(source).as_posix()
        stat = path.stat()
        item = {"id": relative, "relative_path": relative, "source_root": str(source),
                "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "kind": "video" if suffix in VIDEO_EXTENSIONS else "image"}
        try:
            if item["kind"] == "image":
                with Image.open(path) as image:
                    corrected = ImageOps.exif_transpose(image)
                    item.update(width=corrected.width, height=corrected.height,
                                duration_s=None, fps=None, has_audio=False)
            else:
                metadata = probe(path)
                streams = metadata.get("streams", [])
                video = next((s for s in streams if s.get("codec_type") == "video"), None)
                if video is None:
                    raise AnalysisError("Видеопоток отсутствует")
                duration = float(video.get("duration") or metadata.get("format", {}).get("duration", 0))
                if not math.isfinite(duration) or duration <= 0:
                    raise AnalysisError("Не удалось определить длительность")
                fraction = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
                numerator, denominator = (float(v) for v in fraction.split("/"))
                item.update(width=video["width"], height=video["height"],
                            duration_s=duration, fps=numerator / denominator if denominator else None,
                            has_audio=any(s.get("codec_type") == "audio" for s in streams),
                            video_codec=video.get("codec_name"), probe=metadata)
            media.append(item)
        except (AnalysisError, OSError, ValueError, KeyError) as exc:
            errors.append({"source_id": relative, "error": str(exc)})
    return {"schema_version": SCHEMA_VERSION, "source_root": str(source),
            "created_at": now(), "media": media, "errors": errors}


def select_profile(index: dict, profile: str) -> list[dict]:
    if profile == "sample":
        return [dict(item) for item in index["media"]]
    targets = {"GX010279.MP4", "DJI_20260905101930_0007_D.MP4"}
    videos = [dict(item) for item in index["media"]
              if item["kind"] == "video" and Path(item["relative_path"]).name in targets]
    found = {Path(item["relative_path"]).name for item in videos}
    if found != targets:
        raise AnalysisError(f"Для smoke не найдены: {sorted(targets - found)}")
    images = sorted((dict(item) for item in index["media"] if item["kind"] == "image"),
                    key=lambda item: item["relative_path"].casefold())[:2]
    if len(images) != 2:
        raise AnalysisError("Для smoke нужны как минимум две фотографии")
    return sorted(videos + images, key=lambda item: item["relative_path"].casefold())


def windows(duration: float, length: float = 10.0) -> list[tuple[float, float]]:
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(length) or length <= 0:
        raise AnalysisError("Длительности должны быть положительными конечными числами")
    count = math.ceil(duration / length)
    return [(round(i * length, 6), round(min(duration, (i + 1) * length), 6)) for i in range(count)]


def frame_manifest_valid(value: dict, folder: Path) -> bool:
    return bool(value.get("frames")) and all((folder / frame["file"]).is_file()
                                           for frame in value["frames"])


def prepare_frames(media: dict, start: float, end: float, cache: Path,
                   fps: float, max_side: int, decode: str) -> dict:
    settings = {"start_s": start, "end_s": end, "fps": fps, "max_side": max_side,
                "decode": decode, "preparation_version": 1, "ffmpeg": tool_version("ffmpeg")}
    key = task_key("frames", media, settings)
    folder = cache / "frames" / key
    manifest_path = folder / "manifest.json"
    if manifest_path.is_file():
        try:
            existing = load_json(manifest_path)
            if frame_manifest_valid(existing, folder):
                return {**existing, "cache_hit": True, "folder": str(folder)}
        except (ValueError, OSError, KeyError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(media["source_root"]) / media["relative_path"]
    arguments = [executable("ffmpeg"), "-hide_banner", "-nostdin", "-y"]
    if decode == "cuda":
        arguments += ["-hwaccel", "cuda"]
    arguments += ["-ss", str(start), "-t", str(end - start), "-i", source,
                  "-map", "0:v:0", "-an", "-sn",
                  "-vf", f"fps={fps}:start_time=0:round=near:eof_action=pass,scale={max_side}:{max_side}:"
                  "force_original_aspect_ratio=decrease:force_divisible_by=2,showinfo",
                  "-fps_mode", "passthrough", "-q:v", "3", folder / "frame-%05d.jpg"]
    began = time.perf_counter()
    process = command(arguments, timeout=300)
    log = process.stderr.decode("utf-8", errors="replace")
    (folder / "ffmpeg.log").write_text(log, encoding="utf-8")
    points = re.findall(r"\bn:\s*(\d+)\s+pts:\s*[-\d]+\s+pts_time:\s*([-\d.eE+]+)", log)
    frames = []
    for number, pts in points:
        offset = float(pts)
        position = round(start + offset, 6)
        if position >= end:
            continue
        name = f"frame-{int(number) + 1:05d}.jpg"
        if not (folder / name).is_file():
            raise AnalysisError(f"FFmpeg не создал кадр {name}")
        frames.append({"file": name, "source_time_s": position})
    if not frames:
        raise AnalysisError(f"Не извлечены кадры: {media['id']} [{start}, {end}]")
    value = {"key": key, "settings": settings, "frames": frames,
             "duration_s": time.perf_counter() - began}
    atomic_json(manifest_path, value)
    return {**value, "cache_hit": False, "folder": str(folder)}


def prepare_audio(media: dict, duration: float, cache: Path) -> dict:
    settings = {"duration_s": duration, "sample_rate": 16000, "channels": 1,
                "preparation_version": 1, "ffmpeg": tool_version("ffmpeg")}
    key = task_key("audio", media, settings)
    folder = cache / "audio" / key
    wav = folder / "audio.wav"
    manifest = folder / "manifest.json"
    if manifest.is_file() and wav.is_file():
        existing = load_json(manifest)
        if wav.stat().st_size == existing.get("size_bytes"):
            return {**existing, "path": str(wav), "cache_hit": True}
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(media["source_root"]) / media["relative_path"]
    began = time.perf_counter()
    command([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", source, "-t", str(duration), "-vn", "-sn", "-map", "0:a:0",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], timeout=300)
    value = {"key": key, "settings": settings, "size_bytes": wav.stat().st_size,
             "duration_s": time.perf_counter() - began}
    atomic_json(manifest, value)
    return {**value, "path": str(wav), "cache_hit": False}
