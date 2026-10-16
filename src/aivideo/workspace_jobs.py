"""A single durable FIFO for all managed projects and environment maintenance."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .common import AnalysisError, ROOT, atomic_json, load_json, now, offline_environment
from .editor_jobs import JobQueue
from .job_paths import read_job, write_job


class WorkspaceQueue:
    def __init__(self, registry, python=None):
        self.registry = registry
        self.python = Path(python or ROOT / ".venv/Scripts/python.exe")
        self.root = registry.root / "jobs"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.active = None
        self.thread = threading.Thread(target=self.loop, name="workspace-queue", daemon=True)
        for path in self.paths():
            job = read_job(path)
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted")
                write_job(path, job)

    def paths(self):
        paths = list(self.root.glob("*/job.json"))
        for project in self.registry.projects.values():
            if project.root.is_dir():
                paths += list(project.folder("jobs").glob("*/job.json"))
        return paths

    def path(self, key):
        if not isinstance(key, str) or len(key) != 32 or any(c not in "abcdef0123456789" for c in key):
            raise AnalysisError("Неизвестное задание")
        for path in self.paths():
            if path.parent.name == key:
                return path
        raise AnalysisError("Задание недоступно")

    def list(self, project_id=None):
        with self.lock:
            result = []
            for path in self.paths():
                job = read_job(path)
                if project_id is not None and job.get("project_id") != project_id:
                    continue
                progress = path.parent / "progress.json"
                if progress.exists():
                    job["progress"] = load_json(progress)
                render = Path(job.get("output", "")) / "render.json"
                if job["kind"] == "render" and render.is_file():
                    state = load_json(render)
                    job["progress"] = {"stage": "Монтаж и сведение", "done": len(state["shots"]),
                        "total": state.get("total_sections", job["total_shots"]),
                        "cached": sum(s["cache_hit"] for s in state["shots"])}
                result.append(job)
            return sorted(result, key=lambda j: j["sequence"], reverse=True)

    def enqueue(self, kind, project=None, **parameters):
        with self.lock:
            for job in self.list():
                if (job["kind"] == kind and job.get("project_id") == (project.id if project else None)
                        and job["status"] in ("queued", "running") and all(job.get(k) == v for k,v in parameters.items())):
                    return job
            key = uuid.uuid4().hex
            folder = (project.folder("jobs") if project else self.root) / key
            job = {"id": key, "kind": kind, "created_at": now(), "sequence": time.time_ns(),
                   "status": "queued", **parameters}
            if project:
                job.update(manifest=str(project.path), project_id=project.id)
            write_job(folder / "job.json", job, project.id if project else None)
            return job

    def pause(self, key):
        with self.lock:
            path = self.path(key)
            job = read_job(path)
            if job["status"] == "running":
                (path.parent / "pause").touch()
            elif job["status"] == "queued":
                job["status"] = "paused"
                write_job(path, job)
            return job

    def resume(self, key):
        with self.lock:
            path = self.path(key)
            job = read_job(path)
            if job["status"] not in ("paused", "interrupted", "failed"):
                raise AnalysisError("Задание нельзя продолжить")
            (path.parent / "pause").unlink(missing_ok=True)
            job.update(status="queued")
            job.pop("error", None)
            write_job(path, job)
            return job

    def start(self):
        self.thread.start()

    def loop(self):
        while not self.stop.wait(.15):
            with self.lock:
                pending = [j for j in self.list() if j["status"] == "queued"]
                if not pending:
                    continue
                job = min(pending, key=lambda j: j["sequence"])
                path = self.path(job["id"])
                job.update(status="running", started_at=now())
                write_job(path, job)
                self.active = job["id"]
            try:
                maintenance = job["kind"] in ("diagnostics", "install")
                python = Path(sys.executable) if maintenance else self.python
                if not python.is_file():
                    raise AnalysisError("Среда обработки не установлена. Откройте диагностику и установите компоненты.")
                env = offline_environment()
                env["PYTHONPATH"] = str(ROOT / "src")
                env["AIVIDEO_PAUSE_FILE"] = str(path.parent / "pause")
                env["AIVIDEO_PROGRESS_FILE"] = str(path.parent / "progress.json")
                if job.get("project_id"):
                    temporary = self.registry.get(job["project_id"]).folder("tmp")
                    env.update(TEMP=str(temporary), TMP=str(temporary))
                module = "aivideo.editor_worker" if job["kind"] in ("render", "proxy", "thumbnail", "transition_preview") else "aivideo.workspace_worker"
                with (path.parent / "worker.log").open("ab") as log:
                    process = subprocess.Popen([str(python), "-u", "-m", module, "--job", str(path)],
                                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                    process.wait()
                current = read_job(path)
                if current["status"] == "running":
                    current.update(status="interrupted", error="Процесс завершился до записи результата; задание можно продолжить")
                    write_job(path, current)
            except Exception as exc:
                job.update(status="failed", error=str(exc), finished_at=now())
                write_job(path, job)
            finally:
                with self.lock:
                    self.active = None

    def close(self):
        self.stop.set()
        with self.lock:
            if self.active:
                self.pause(self.active)
        if self.thread.is_alive():
            self.thread.join(timeout=20)
        if self.thread.is_alive():
            raise AnalysisError("Обработчик останавливается. Оставьте терминал открытым.")


class ProjectQueue(JobQueue):
    """Existing editor producers submit into the workspace scheduler."""
    def __init__(self, store, scheduler):
        self.store, self.scheduler = store, scheduler
        self.project = store.project
        self.root = self.project.folder("jobs")
        self.cache = self.project.folder("cache") / "render"
        self.lock = scheduler.lock

    def _write(self, path, job):
        write_job(path, job, self.project.id)

    def list(self):
        return self.scheduler.list(self.project.id)

    def path(self, key):
        path = self.scheduler.path(key)
        if path.parents[1] != self.root:
            raise AnalysisError("Задание относится к другому проекту")
        return path

    def add_render(self, variant, expected):
        with self.lock:
            job = super().add_render(variant, expected)
            job["output"] = str(self.project.folder("renders") / job["id"])
            self._write(self.path(job["id"]), job)
            return job

    def pause(self, key):
        self.path(key)
        return self.scheduler.pause(key)

    def resume(self, key):
        self.path(key)
        return self.scheduler.resume(key)

    def close(self):
        pass
