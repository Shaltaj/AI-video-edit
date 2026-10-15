from __future__ import annotations

import json
import platform
from pathlib import Path
import subprocess
import sys
import time

from .common import (AnalysisError, ROOT, RUNS, SCHEMA_VERSION, atomic_json, atomic_text,
                     cached_result, digest, load_json, now, offline_environment,
                     task_key, tool_version, versions)
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
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
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


def result_references(run: dict) -> tuple[list[dict], list[dict]]:
    cache = Path(run["cache"])
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
            fps: float = 2.0, max_side: int = 512, decode: str = "cuda") -> dict:
    from .report import report

    output = output.resolve()
    source = source.resolve()
    if output == source or source in output.parents:
        raise AnalysisError("Результаты должны находиться вне папки исходников")
    output.mkdir(parents=True, exist_ok=True)
    cache = RUNS / "cache" / "results"
    preparation_cache = RUNS / "cache" / "prepared"
    began = time.perf_counter()
    run = {"schema_version": SCHEMA_VERSION, "created_at": now(), "status": "running",
           "profile": profile, "source_root": str(source), "output": str(output), "cache": str(cache),
           "visual_jobs": [], "asr_jobs": [], "preparation": [], "errors": [], "phases": [],
           "cache_stats": {},
           "versions": versions(), "settings": {"window_s": window_s, "fps": fps,
                                                  "max_side": max_side, "decode": decode}}
    try:
        diagnostic = doctor(output / "doctor")
        models = diagnostic["models"]
        run["models"] = models
        index = scan(source)
        atomic_json(output / "media-index.json", index)
        if index["errors"]:
            raise AnalysisError(f"Ошибки чтения исходников: {index['errors']}")
        selected = select_profile(index, profile)
        if not selected:
            raise AnalysisError("В подборке нет поддерживаемых материалов")
        run["media"] = selected
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
        atomic_json(output / "run.json", run)
        for media in selected:
            print(f"[prepare] {media['id']}", flush=True)
            if media["kind"] == "image":
                key = task_key("visual-image", media, visual_settings, model_fingerprints["visual"])
                run["visual_jobs"].append({"key": key, "kind": "image", "source_id": media["id"],
                    "frames": [{"path": str(source / media["relative_path"]), "source_time_s": None}]})
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
            atomic_json(output / "run.json", run)
        for kind, settings in (("asr", asr_settings), ("visual", visual_settings)):
            jobs = run[f"{kind}_jobs"]
            pending = [item for item in jobs if not cached_result(cache, item["key"])]
            run["cache_stats"][kind] = {"total": len(jobs), "computed": len(pending),
                                        "cached": len(jobs) - len(pending)}
            print(f"[{kind}] pending={len(pending)}, cached={len(jobs) - len(pending)}", flush=True)
            if pending:
                phase_output = output / f"{kind}-phase.json"
                run_worker({"mode": kind, "items": pending, "settings": settings,
                            "model": models[kind], "cache": str(cache),
                            "phase_output": str(phase_output)}, output)
                run["phases"].append(load_json(phase_output))
            atomic_json(output / "run.json", run)
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
        atomic_json(output / "run.json", run)
        report(output)
    return run
