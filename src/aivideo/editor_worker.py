"""One durable local task per subprocess; GPU resources die with the worker."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from .common import atomic_json, executable, load_json, now
from .render import RenderPaused, render, run_ffmpeg


def execute(job_path: Path) -> None:
    job = load_json(job_path)
    began = time.perf_counter()
    try:
        if job["kind"] == "render":
            state = render(Path(job["snapshot"]), Path(job["output"]), cache=Path(job["cache"]))
            job["result"] = {"frames": state["validation"]["frames"],
                             "duration_s": state["duration_s"], "shots": job["total_shots"],
                             "sections": len(state["shots"]), "transitions": state["transition_count"],
                             "cache_counts": state["cache_counts"],
                             "cached_shots": state["cache_counts"]["body"]["cached"],
                             "cached_sections": sum(s["cache_hit"] for s in state["shots"])}
        elif job["kind"] == "transition_preview":
            from .render import load_plan
            from .transitions import render_preview
            job["result"] = render_preview(load_plan(Path(job["snapshot"])), job["from_shot_id"], job["to_shot_id"],
                                           Path(job["target"]), Path(job["cache"]), job_path.parent / "progress.json")
        elif job["kind"] == "proxy":
            target = Path(job["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".partial.mp4")
            media = job["media"]
            stat = Path(media["path"]).stat()
            if (stat.st_size, stat.st_mtime_ns) != (media["size_bytes"], media["mtime_ns"]):
                raise ValueError("Исходник изменился; обновите анализ")
            args = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
            if job.get("decode", "cuda") == "cuda":
                args += ["-hwaccel", "cuda"]
            args += ["-i", media["path"], "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                     "-vf", "fps=30,scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,format=yuv420p",
                     "-c:v", job.get("encoder", "h264_nvenc")]
            if job.get("encoder", "h264_nvenc") == "h264_nvenc":
                args += ["-preset", "p4", "-cq", "28", "-b:v", "2500k", "-maxrate", "4000k", "-bufsize", "8000k"]
            else:
                args += ["-preset", "fast", "-crf", "28", "-threads", "2"]
            args += ["-g", "30", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                     "-map_metadata", "-1", "-movflags", "+faststart", temporary]
            try:
                run_ffmpeg(args, job_path.parent / "proxy.log", job_path.parent)
                from .media import probe
                metadata = probe(temporary)
                video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
                if abs(float(metadata["format"]["duration"]) - media["duration_s"]) > 0.15 or video["codec_name"] != "h264":
                    raise ValueError("Прокси не соответствует исходнику")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            job["result"] = {"size_bytes": target.stat().st_size}
        elif job["kind"] == "thumbnail":
            target = Path(job["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.stem + ".partial.jpg")
            try:
                run_ffmpeg([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-ss", job["time_s"], "-i", job["source"], "-frames:v", "1",
                            "-vf", "scale=320:320:force_original_aspect_ratio=decrease", temporary],
                           job_path.parent / "thumbnail.log", job_path.parent)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            raise ValueError("Неизвестный тип задания")
        job["status"] = "complete"
    except (RenderPaused, KeyboardInterrupt):
        job["status"] = "paused"
    except Exception as exc:
        job.update(status="failed", error=str(exc))
        import traceback
        traceback.print_exc()
    finally:
        job["wall_time_s"] = round(time.perf_counter() - began, 3)
        job["finished_at"] = now()
        atomic_json(job_path, job)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    execute(parser.parse_args().job)
