"""Loopback-only API and media server for the local editor."""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from .common import AnalysisError, RUNS, digest, load_json
from .editor_jobs import JobQueue
from .editor_store import Conflict, EditorStore, movie_digest

WEB = Path(__file__).parent / "web"


class EditorApp:
    def __init__(self, plan: Path, *, state_root=None, cache=None, proxy_options=None):
        self.store = EditorStore(plan, state_root)
        self.lease = (self.store.root / "server.lock").open("a+b")
        self.lease.seek(0)
        if not self.lease.read(1):
            self.lease.write(b"0")
            self.lease.flush()
        self.lease.seek(0)
        try:
            import msvcrt
            msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.lease.close()
            raise AnalysisError("Редактор этого проекта уже запущен в другом терминале") from exc
        self.jobs = JobQueue(self.store, cache=cache)
        self.token = secrets.token_urlsafe(32)
        self.proxy_options = proxy_options or {}
        self.assets: dict[str, Path] = {}
        self.prepared = []
        run = self.store.analysis / "run.json"
        if run.exists():
            self.prepared = load_json(run).get("preparation", [])
        self.catalog = [{**item, "id": digest([item["source_id"], item.get("start_s"), item.get("end_s")])[:20]}
                        for item in self.store.visual]
        self.baselines = []
        # Import existing successful standalone renders as previous drafts.
        for path in RUNS.rglob("render.json"):
            if self.store.root in path.parents:
                continue
            try:
                state = load_json(path)
                snapshot = path.parent / "plan.json"
                if state["status"] == "complete" and snapshot.exists() and (path.parent / "film.mp4").exists():
                    spec = load_json(snapshot)
                    if (snapshot.parent / spec["analysis"]).resolve() == self.store.analysis:
                        self.baselines.append({"path": path.parent / "film.mp4", "digest": movie_digest(spec, snapshot),
                                               "plan_path": state.get("plan"), "time": path.stat().st_mtime_ns})
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def asset(self, path: Path) -> str:
        path = path.resolve()
        key = digest(str(path))[:32]
        self.assets[key] = path
        return "/media/" + key

    def thumbnail_path(self, entry: dict) -> Path | None:
        media = self.store.library[entry["source_id"]]
        if media["kind"] == "image":
            return self.store.source / media["relative_path"]
        point = entry.get("start_s") or 0
        candidates = []
        for preparation in self.prepared:
            if preparation.get("source_id") != media["id"] or preparation.get("kind") != "frames":
                continue
            for frame in preparation.get("frames", []):
                path = Path(preparation["folder"]) / frame["file"]
                if path.is_file():
                    candidates.append((abs(frame["source_time_s"] - point), path))
        return min(candidates, key=lambda x: x[0])[1] if candidates else None

    def catalog_payload(self):
        result = []
        for entry in self.catalog:
            path = self.thumbnail_path(entry)
            result.append({k: entry.get(k) for k in ("id", "kind", "source_id", "start_s", "end_s", "description", "uncertainties")}
                          | {"thumbnail": self.asset(path) if path else None})
        sources = [{k: m[k] for k in ("id", "kind", "duration_s", "has_audio", "width", "height")}
                   for m in self.store.library.values()]
        return {"entries": result, "sources": sources}

    def get_plan(self, variant: str):
        value = self.store.get(variant)
        current = movie_digest(value["plan"], self.store.path(variant))
        movies = [{"path": Path(j["output"]) / "film.mp4", "digest": j["movie_digest"], "time": (j["finished_at"], j.get("sequence", 0))}
                  for j in self.jobs.list() if j["kind"] == "render" and j["status"] == "complete" and j["variant"] == variant]
        if movies:
            selected = max(movies, key=lambda m: m["time"])
        else:
            baseline = [m for m in self.baselines if m["plan_path"] == str(self.store.path(variant)) or m["digest"] == current]
            selected = max(baseline, key=lambda m: m["time"]) if baseline else None
        value["movie"] = ({"url": self.asset(selected["path"]), "stale": selected["digest"] != current}
                          if selected and selected["path"].exists() else None)
        return value

    def close(self):
        try:
            self.jobs.close()
        finally:
            self.lease.close()

    def proxy_payload(self, job: dict):
        value = dict(job)
        if job["status"] == "complete":
            value["url"] = self.asset(Path(job["target"]))
        return value


class EditorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, EditorHandler)


class EditorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_):
        pass

    @property
    def app(self):
        return self.server.app

    def headers_for(self, status, size, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()

    def send_bytes(self, data: bytes, content_type="application/json; charset=utf-8", status=200):
        self.headers_for(status, len(data), content_type)
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), status=status)

    def media(self, path: Path):
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                self.headers_for(416, 0, "application/octet-stream", {"Content-Range": f"bytes */{size}"})
                return
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                start, end = max(0, size - int(last)), size - 1
            if start > end or start >= size or size == 0:
                self.headers_for(416, 0, "application/octet-stream", {"Content-Range": f"bytes */{size}"})
                return
            status = 206
        extra = {"Accept-Ranges": "bytes"}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.headers_for(status, end - start + 1, mimetypes.guess_type(path.name)[0] or "application/octet-stream", extra)
        if self.command != "HEAD":
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.handle_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (AnalysisError, OSError, ValueError, KeyError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def handle_get(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
            return self.send_json({"error": "Используйте адрес 127.0.0.1"}, 403)
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        variant = query.get("variant", [self.app.store.default_id])[0]
        if route.path == "/":
            page = (WEB / "index.html").read_text(encoding="utf-8").replace("__EDITOR_TOKEN__", html.escape(self.app.token))
            return self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        if route.path in ("/app.js", "/app.css"):
            file = WEB / route.path[1:]
            return self.send_bytes(file.read_bytes(), "text/javascript; charset=utf-8" if file.suffix == ".js" else "text/css; charset=utf-8")
        if route.path.startswith("/media/"):
            path = self.app.assets.get(route.path.removeprefix("/media/"))
            if not path or not path.is_file():
                return self.send_json({"error": "Материал не найден"}, 404)
            return self.media(path)
        if route.path == "/api/project":
            return self.send_json({"default_variant": self.app.store.default_id, "variants": self.app.store.variants()})
        if route.path == "/api/catalog":
            return self.send_json(self.app.catalog_payload())
        if route.path == "/api/plan":
            return self.send_json(self.app.get_plan(variant))
        if route.path == "/api/history":
            return self.send_json(self.app.store.history(variant))
        if route.path == "/api/jobs":
            return self.send_json([self.app.proxy_payload(j) if j["kind"] == "transition_preview" else j
                                   for j in self.app.jobs.list()])
        return self.send_json({"error": "Не найдено"}, 404)

    def do_POST(self):
        try:
            host = self.headers.get("Host", "")
            expected_host = f"127.0.0.1:{self.server.server_address[1]}"
            if host != expected_host or self.headers.get("X-Editor-Token") != self.app.token:
                self.close_connection = True
                return self.send_json({"error": "Запрос не из локального редактора"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{expected_host}":
                self.close_connection = True
                return self.send_json({"error": "Недопустимый источник запроса"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2 * 1024 * 1024:
                return self.send_json({"error": "Недопустимый размер запроса"}, 413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise AnalysisError("Ожидался объект JSON")
            route = urlsplit(self.path).path
            variant = body.get("variant", self.app.store.default_id)
            if route == "/api/plan":
                self.app.store.save(variant, body["plan"], body["revision"])
                value = self.app.get_plan(variant)
            elif route == "/api/variants":
                value = self.app.store.create(variant, body["name"], body["plan"])
            elif route == "/api/restore":
                self.app.store.restore(variant, body["history_revision"], body["revision"])
                value = self.app.get_plan(variant)
            elif route == "/api/render":
                value = self.app.jobs.add_render(variant, body["revision"])
            elif route == "/api/proxy":
                value = self.app.proxy_payload(self.app.jobs.proxy(body["source_id"], **self.app.proxy_options))
            elif route == "/api/transition-preview":
                value = self.app.proxy_payload(self.app.jobs.transition_preview(
                    variant, body["revision"], body["from_shot_id"], body["to_shot_id"]))
            elif route == "/api/thumbnail":
                entry = next((e for e in self.app.catalog if e["id"] == body["entry_id"]), None)
                if not entry:
                    raise AnalysisError("Неизвестная сцена каталога")
                value = self.app.proxy_payload(self.app.jobs.thumbnail(entry["source_id"], entry["start_s"]))
            elif route == "/api/pause":
                value = self.app.jobs.pause(body["id"])
            elif route == "/api/resume":
                value = self.app.jobs.resume(body["id"])
            else:
                return self.send_json({"error": "Не найдено"}, 404)
            self.send_json(value)
        except Conflict as exc:
            self.send_json({"error": str(exc)}, 409)
        except (AnalysisError, OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            self.send_json({"error": str(exc)}, 400)


def serve(plan: Path, port: int):
    app = EditorApp(plan)
    try:
        server = EditorHTTPServer(("127.0.0.1", port), app)
    except BaseException:
        app.close()
        raise
    app.jobs.start()
    print(f"Editor: http://127.0.0.1:{server.server_address[1]}\nPlan: {plan.resolve()}\nCtrl+C: stop and retain jobs", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
