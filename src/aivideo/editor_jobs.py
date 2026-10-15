from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .common import AnalysisError, ROOT, RUNS, atomic_json, digest, load_json, now, offline_environment
from .editor_store import EditorStore, movie_digest


class JobQueue:
    def __init__(self, store: EditorStore, *, cache: Path | None = None):
        self.store = store
        self.root = store.root / "jobs"
        self.root.mkdir(exist_ok=True)
        self.cache = (cache or RUNS / "render-cache").resolve()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.process = None
        self.active = None
        for path in self.root.glob("*/job.json"):
            job = load_json(path)
            if job["status"] in ("running", "queued"):
                job["status"] = "interrupted"
                atomic_json(path, job)
        self.thread = threading.Thread(target=self.loop, name="editor-queue", daemon=True)

    def start(self):
        self.thread.start()

    def path(self, key: str) -> Path:
        if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
            raise AnalysisError("Неизвестное задание")
        path = self.root / key / "job.json"
        if not path.is_file():
            raise AnalysisError("Задание не найдено")
        return path

    def list(self) -> list[dict]:
        with self.lock:
            jobs = [load_json(path) for path in self.root.glob("*/job.json")]
        for job in jobs:
            progress = Path(job.get("output", "")) / "render.json"
            if job["kind"] == "render" and progress.is_file():
                state = load_json(progress)
                job["progress"] = {"done": len(state.get("shots", [])), "total": job["total_shots"],
                                   "stage": "сцены" if len(state.get("shots", [])) < job["total_shots"] else "сведение и проверка",
                                   "cached": sum(s["cache_hit"] for s in state.get("shots", []))}
        return sorted(jobs, key=lambda j: (j["created_at"], j.get("sequence", 0)), reverse=True)

    def add_render(self, variant: str, expected: str) -> dict:
        with self.lock:
            for pending in self.list():
                if (pending["kind"] == "render" and pending.get("variant") == variant
                        and pending.get("revision") == expected and pending["status"] in ("queued", "running")):
                    return pending
            key = uuid.uuid4().hex
            folder = self.root / key
            value = self.store.snapshot(variant, folder / "plan.json", expected)
            job = {"id": key, "kind": "render", "status": "queued", "created_at": now(),
                   "sequence": time.time_ns(),
                   "variant": variant, "revision": value["revision"],
                   "movie_digest": movie_digest(value["plan"], self.store.path(variant)),
                   "snapshot": str(folder / "plan.json"), "output": str(folder / "result"),
                   "cache": str(self.cache), "total_shots": len(value["plan"]["shots"])}
            atomic_json(folder / "job.json", job)
            return job

    def proxy(self, source_id: str, *, decode="cuda", encoder="h264_nvenc") -> dict:
        with self.lock:
            if source_id not in self.store.library:
                raise AnalysisError("Неизвестный исходник")
            media = self.store.library[source_id]
            if media["kind"] != "video":
                raise AnalysisError("Прокси нужен только для видео")
            source = (self.store.source / media["relative_path"]).resolve()
            if self.store.source not in source.parents:
                raise AnalysisError("Путь выходит за папку исходников")
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (media["size_bytes"], media["mtime_ns"]):
                raise AnalysisError("Исходник изменился; обновите анализ")
            fingerprint = digest({"path": str(source), "size": stat.st_size, "mtime": stat.st_mtime_ns,
                                  "version": 1, "decode": decode, "encoder": encoder})
            target = self.store.root / "proxies" / f"{fingerprint}.mp4"
            if target.is_file():
                return {"status": "complete", "target": str(target), "source_id": source_id}
            for job in self.list():
                if job["kind"] == "proxy" and job["fingerprint"] == fingerprint and job["status"] not in ("failed", "complete"):
                    return job
            key = uuid.uuid4().hex
            job = {"id": key, "kind": "proxy", "status": "queued", "created_at": now(),
                   "sequence": time.time_ns(),
                   "fingerprint": fingerprint, "source_id": source_id, "target": str(target),
                   "media": {**{k: v for k, v in media.items() if k != "probe"}, "path": str(source)},
                   "decode": decode, "encoder": encoder}
            atomic_json(self.root / key / "job.json", job)
            return job

    def thumbnail(self, source_id: str, time_s: float) -> dict:
        with self.lock:
            media = self.store.library[source_id]
            if media["kind"] != "video" or not 0 <= time_s < media["duration_s"]:
                raise AnalysisError("Неверный диапазон миниатюры")
            source = (self.store.source / media["relative_path"]).resolve()
            if self.store.source not in source.parents:
                raise AnalysisError("Путь выходит за папку исходников")
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (media["size_bytes"], media["mtime_ns"]):
                raise AnalysisError("Исходник изменился; обновите анализ")
            target = self.store.root / "thumbnails" / (digest([source_id, time_s, stat.st_mtime_ns]) + ".jpg")
            if target.exists():
                return {"status": "complete", "target": str(target)}
            for job in self.list():
                if job["kind"] == "thumbnail" and job["target"] == str(target) and job["status"] not in ("failed", "complete"):
                    return job
            key = uuid.uuid4().hex
            job = {"id": key, "kind": "thumbnail", "status": "queued", "created_at": now(),
                   "sequence": time.time_ns(),
                   "target": str(target), "source": str(source), "time_s": time_s}
            atomic_json(self.root / key / "job.json", job)
            return job

    def pause(self, key: str) -> dict:
        with self.lock:
            path = self.path(key)
            job = load_json(path)
            if job["status"] == "running":
                (path.parent / "pause").touch()
            elif job["status"] == "queued":
                job["status"] = "paused"
                atomic_json(path, job)
            return job

    def resume(self, key: str) -> dict:
        with self.lock:
            path = self.path(key)
            job = load_json(path)
            if job["status"] not in ("paused", "interrupted", "failed"):
                raise AnalysisError("Это задание нельзя продолжить")
            (path.parent / "pause").unlink(missing_ok=True)
            job.update(status="queued")
            job.pop("error", None)
            atomic_json(path, job)
            return job

    def loop(self):
        while not self.stop.wait(0.15):
            with self.lock:
                pending = [j for j in self.list() if j["status"] == "queued"]
                if not pending:
                    continue
                # Explicit renders take priority over pending proxy work.
                job = min(pending, key=lambda j: (j["kind"] != "render", j["created_at"], j.get("sequence", 0)))
                path = self.path(job["id"])
                job.update(status="running", started_at=now())
                atomic_json(path, job)
                self.active = job["id"]
                env = offline_environment()
                env["AIVIDEO_PAUSE_FILE"] = str(path.parent / "pause")
                with (path.parent / "worker.log").open("ab") as log:
                    self.process = subprocess.Popen([sys.executable, "-m", "aivideo.editor_worker", "--job", str(path)],
                                                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            self.process.wait()
            with self.lock:
                current = load_json(path)
                if current["status"] == "running":
                    current.update(status="interrupted", error="Процесс обработки завершился до записи результата")
                    atomic_json(path, current)
                self.active, self.process = None, None

    def close(self):
        self.stop.set()
        with self.lock:
            if self.active:
                self.pause(self.active)
        if self.thread.is_alive():
            self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise AnalysisError("Обработчик ещё останавливается; оставьте терминал открытым")
