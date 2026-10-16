"""Subprocess entrypoint for project analysis and environment maintenance."""
import argparse
from pathlib import Path
import time
import traceback

from .common import AnalysisError, atomic_json, now, progress
from .job_paths import read_job, write_job


def execute(path):
    job = read_job(path)
    began = time.monotonic()
    try:
        if job["kind"] in ("diagnostics","install"):
            from .installation import diagnose, install
            output = Path(job["output"])
            result = install(job["components"],output) if job["kind"]=="install" else diagnose(output)
            job["result"] = result
        else:
            from .projects import Project
            project = Project(Path(job["manifest"]))
            if job["kind"] == "scan":
                from .media import scan
                progress("Чтение исходников")
                index = scan(project.source, project.read().get("excluded_sources", []))
                atomic_json(project.folder("analysis")/"media-index.json",index)
                job["result"] = {"files":len(index["media"]),"errors":index["errors"]}
                progress("Список исходников готов",len(index["media"]),len(index["media"]))
            elif job["kind"] == "analyze":
                from .pipeline import analyze
                state = analyze(project.source,project.folder("analysis"),"sample",
                                cache_root=project.folder("cache")/"analysis",relative_paths=True,
                                excluded_sources=project.read().get("excluded_sources", []),**job["settings"])
                job["result"] = {"status":state["status"],"cache_stats":state["cache_stats"],
                                 "skipped_sources":state["skipped_sources"],"excluded_sources":state["excluded_sources"]}
            else:
                raise AnalysisError("Неизвестный тип задания")
        job["status"] = "complete"
    except KeyboardInterrupt:
        job["status"] = "paused"
    except Exception as exc:
        job.update(status="failed",error=str(exc))
        traceback.print_exc()
    finally:
        job.update(finished_at=now(),wall_time_s=round(time.monotonic()-began,3))
        write_job(path,job)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--job",type=Path,required=True)
    execute(parser.parse_args().job)
