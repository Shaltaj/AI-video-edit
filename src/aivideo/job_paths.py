"""Keep managed job paths relative on disk and absolute only at execution."""
from pathlib import Path
from .common import atomic_json, load_json

PATH_FIELDS = ("snapshot", "output", "cache", "target", "progress_file", "manifest")


def read_job(path):
    job = load_json(path)
    if job.get("workspace_paths"):
        root = Path(path).resolve().parents[2]
        for key in PATH_FIELDS:
            if key in job:
                resolved = (root / job[key]).resolve()
                if root not in resolved.parents:
                    raise ValueError("Путь задания выходит за рабочую папку")
                job[key] = str(resolved)
    return job


def write_job(path, job, project_id=None):
    value = dict(job)
    if project_id:
        value.update(project_id=project_id, workspace_paths=True)
    if value.get("workspace_paths"):
        root = Path(path).resolve().parents[2]
        for key in PATH_FIELDS:
            if key in value and Path(value[key]).is_absolute():
                value[key] = Path(value[key]).resolve().relative_to(root).as_posix()
    atomic_json(path, value)
