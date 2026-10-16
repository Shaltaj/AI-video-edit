from __future__ import annotations

import json
import platform
from pathlib import Path
import subprocess
import sys
import time
import threading

from .common import (AnalysisError, ROOT, RUNS, SCHEMA_VERSION, atomic_json, atomic_text,
                     cached_result, digest, load_json, now, offline_environment,
                     task_key, tool_version, versions, check_pause, progress)
from .media import prepare_audio, prepare_frames, scan, select_profile, windows
from .models import model_manifest
from .worker import VISUAL_PROMPT


def run_worker(job: dict, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    mode = job["mode"]
    job = {**job, "error_output": str(folder / f"{mode}-error.json")}
    Path(job["error_output"]).unlink(missing_ok=True)
    job_path = folder / f"{mode}-job.json"
    atomic_json(job_path, job)
    with (folder / f"{mode}-worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-u", "-m", "aivideo.worker", str(job_path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace",
                                   env=offline_environment(), cwd=ROOT)
        def copy_output():
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
        reader = threading.Thread(target=copy_output, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                check_pause()
                time.sleep(.1)
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            reader.join(timeout=5)
        if code:
            raise AnalysisError(f"Этап {mode} завершился с кодом {code}. "
                                f"Диагностика: {folder / (mode + '-error.json')}; "
                                f"журнал: {folder / (mode + '-worker.log')}")


def doctor(output: Path, require_models: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": SCHEMA_VERSION, "created_at": now(), "versions": versions(),
              "python": sys.version, "platform": platform.platform()}
    try:
        result["tools"] = {name: tool_version(name) for name in ("ffmpeg", "ffprobe")}
        gpu_output = output / "gpu.json"
        run_worker({"mode": "doctor", "output": str(gpu_output)}, output)
        result["gpu"] = load_json(gpu_output)
        if require_models:
            result["models"] = model_manifest()
        result["status"] = "complete"
    except Exception as exc:
        result.update(status="error", error=str(exc))
        atomic_json(output / "doctor.json", result)
        raise
    atomic_json(output / "doctor.json", result)
    return result


def result_references(run: dict, folder: Path | None = None) -> tuple[list[dict], list[dict]]:
    cache = Path(run["cache"])
    if run.get("relative_paths"):
        if folder is None:
            raise AnalysisError("Для относительного каталога укажите папку анализа")
        cache = (folder / cache).resolve()
    visual, transcripts = [], []
    for job in run.get("visual_jobs", []):
        value = cached_result(cache, job["key"])
        if value:
            visual.append(value)
    for job in run.get("asr_jobs", []):
        value = cached_result(cache, job["key"])
        if value:
            transcripts.append(value)
    return visual, transcripts


def analyze(source: Path, output: Path, profile: str, *, window_s: float = 10.0,
            fps: float = 2.0, max_side: int = 512, decode: str = "cuda", cache_root: Path | None = None,
            relative_paths: bool = False, excluded_sources=()) -> dict:
    from .report import report

    output = output.resolve()
    source = source.resolve()
    if output == source or source in output.parents:
        raise AnalysisError("Результаты должны находиться вне папки исходников")
    output.mkdir(parents=True, exist_ok=True)
    cache_root = (cache_root or RUNS / "cache").resolve()
    if cache_root == source or source in cache_root.parents:
        raise AnalysisError("Кеш должен находиться вне исходников")
    cache = cache_root / "results"
    preparation_cache = cache_root / "prepared"
    previous = load_json(output / "run.json") if (output / "run.json").exists() else {}
    if previous.get("relative_paths"):
        import os
        for item in previous.get("preparation", []):
            for key in ("folder", "path"):
                if key in item:
                    item[key] = str((output / item[key]).resolve())
    began = time.perf_counter()
    run = {"schema_version": SCHEMA_VERSION, "created_at": now(), "status": "running",
           "profile": profile, "source_root": str(source), "output": str(output), "cache": str(cache),
           "visual_jobs": [], "asr_jobs": [], "preparation": previous.get("preparation", []), "errors": [], "phases": [],
           "cache_stats": {},
           "excluded_sources": sorted(excluded_sources), "skipped_sources": [],
           "versions": versions(), "settings": {"window_s": window_s, "fps": fps,
                                                  "max_side": max_side, "decode": decode}}
    def save_run():
        value = run
        if relative_paths:
            import os
            value = json.loads(json.dumps(run))
            value.update(relative_paths=True, cache=os.path.relpath(cache, output).replace("\\", "/"), output=".")
            for item in value["preparation"]:
                for key in ("folder", "path"):
                    if key in item:
                        item[key] = os.path.relpath(item[key], output).replace("\\", "/")
            for job in value["visual_jobs"]:
                for frame in job.get("frames", []):
                    frame["path"] = os.path.relpath(frame["path"], output).replace("\\", "/")
            for job in value["asr_jobs"]:
                if "audio_path" in job:
                    job["audio_path"] = os.path.relpath(job["audio_path"], output).replace("\\", "/")
        atomic_json(output / "run.json", value)
    try:
        progress("Чтение доступных исходников")
        check_pause()
        index = scan(source, excluded_sources)
        atomic_json(output / "media-index.json", index)
        run["skipped_sources"] = index["errors"]
        selected = select_profile(index, profile)
        if not selected:
            raise AnalysisError(f"Нет доступных материалов для анализа. Ошибки чтения: {len(index['errors'])}; исключено: {len(index['excluded'])}. Прочитайте список после исправления файлов или верните исключённые.")
        run["media"] = selected
        progress("Проверка окружения")
        diagnostic = doctor(output / "doctor")
        models = diagnostic["models"]
        run["models"] = models
        run["tools"] = diagnostic["tools"]
        asr_settings = {"language": "ru", "compute_type": "int8_float16", "beam_size": 5,
                        "vad_filter": True, "word_timestamps": True,
                        "condition_on_previous_text": False, "pipeline_version": 1}
        visual_settings = {"max_side": max_side, "fps": fps, "max_new_tokens": 256,
                           "quantization": "nf4", "double_quantization": True,
                           "compute_dtype": "float16", "attention": "sdpa", "pipeline_version": 1,
                           "prompt_digest": digest(VISUAL_PROMPT)}
        model_fingerprints = {kind: {**model, "runtime": run["versions"]}
                              for kind, model in models.items()}
        save_run()
        progress("Подготовка кадров и звука", 0, len(selected))
        for number, media in enumerate(selected, 1):
            check_pause()
            print(f"[prepare] {media['id']}", flush=True)
            if media["kind"] == "image":
                from .images import HEIF_EXTENSIONS, prepare_photo
                image_settings = visual_settings
                photo_path = str(source / media["relative_path"])
                if Path(photo_path).suffix.lower() in HEIF_EXTENSIONS:
                    import importlib.metadata
                    image_settings = {**visual_settings, "photo_version": 1,
                                      "pillow_heif": importlib.metadata.version("pillow-heif")}
                    prepared = prepare_photo(media, preparation_cache)
                    run["preparation"].append({"source_id": media["id"], "kind": "photo", **prepared})
                    photo_path = prepared["path"]
                key = task_key("visual-image", media, image_settings, model_fingerprints["visual"])
                run["visual_jobs"].append({"key": key, "kind": "image", "source_id": media["id"],
                    "frames": [{"path": photo_path, "source_time_s": None}]})
            else:
                duration = min(media["duration_s"], 60.0) if profile == "smoke" else media["duration_s"]
                if media["has_audio"]:
                    key = task_key("asr", media, {**asr_settings, "duration_s": duration,
                                   "ffmpeg": diagnostic["tools"]["ffmpeg"]}, model_fingerprints["asr"])
                    asr_item = {"key": key, "source_id": media["id"], "duration_s": duration}
                    if not cached_result(cache, key):
                        audio = prepare_audio(media, duration, preparation_cache)
                        run["preparation"].append({"source_id": media["id"], "kind": "audio", **audio})
                        asr_item["audio_path"] = audio["path"]
                    run["asr_jobs"].append(asr_item)
                for start, end in windows(duration, window_s):
                    check_pause()
                    settings = {**visual_settings, "start_s": start, "end_s": end,
                                "decode": decode, "ffmpeg": diagnostic["tools"]["ffmpeg"]}
                    key = task_key("visual-video", media, settings, model_fingerprints["visual"])
                    item = {"key": key, "kind": "video", "source_id": media["id"],
                            "start_s": start, "end_s": end}
                    if not cached_result(cache, key):
                        prepared = prepare_frames(media, start, end, preparation_cache, fps, max_side, decode)
                        run["preparation"].append({"source_id": media["id"], "kind": "frames", **prepared})
                        item["frames"] = [{"path": str(Path(prepared["folder"]) / frame["file"]),
                                           "source_time_s": frame["source_time_s"]} for frame in prepared["frames"]]
                    run["visual_jobs"].append(item)
            save_run()
            progress("Подготовка кадров и звука", number, len(selected))
        for kind, settings in (("asr", asr_settings), ("visual", visual_settings)):
            jobs = run[f"{kind}_jobs"]
            pending = [item for item in jobs if not cached_result(cache, item["key"])]
            run["cache_stats"][kind] = {"total": len(jobs), "computed": len(pending),
                                        "cached": len(jobs) - len(pending)}
            print(f"[{kind}] pending={len(pending)}, cached={len(jobs) - len(pending)}", flush=True)
            progress("Речь" if kind == "asr" else "Описание изображения", len(jobs)-len(pending), len(jobs), len(jobs)-len(pending))
            if pending:
                phase_output = output / f"{kind}-phase.json"
                run_worker({"mode": kind, "items": pending, "settings": settings,
                            "model": models[kind], "cache": str(cache),
                            "total": len(jobs), "cached_count": len(jobs)-len(pending),
                            "phase_output": str(phase_output)}, output)
                run["phases"].append(load_json(phase_output))
            save_run()
        visual, transcripts = result_references(run)
        if len(visual) != len(run["visual_jobs"]) or len(transcripts) != len(run["asr_jobs"]):
            raise AnalysisError("Не все операции завершились; прогон нельзя считать готовым")
        run["status"] = "complete"
    except KeyboardInterrupt:
        run["status"] = "interrupted"
        run["errors"].append("Обработка прервана пользователем; завершённые операции сохранены.")
        raise
    except Exception as exc:
        run["status"] = "error"
        run["errors"].append(str(exc))
        raise
    finally:
        run["wall_time_s"] = round(time.perf_counter() - began, 3)
        run["finished_at"] = now()
        save_run()
        report(output)
    return run
