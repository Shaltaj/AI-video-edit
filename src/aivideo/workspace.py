"""Dependency-free web shell around projects and the existing editor."""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
from urllib.parse import urlsplit

from .common import AnalysisError, ROOT, load_json
from .editor import EditorApp, EditorHandler, EditorHTTPServer, WEB
from .editor_store import Conflict
from .handoff import catalog, export_markdown, import_bundle, validate_bundle
from .installation import INSTALLABLE
from .projects import ProjectRegistry, ProjectEditorStore
from .workspace_jobs import WorkspaceQueue, ProjectQueue


class WorkspaceApp:
    def __init__(self, *, state_root=None, app_root=ROOT, python=None, proxy_options=None):
        self.registry = ProjectRegistry(state_root, app_root)
        self.token = secrets.token_urlsafe(32)
        self.editors = {}
        self.proxy_options = proxy_options
        self.lease = (self.registry.root/"server.lock").open("a+b")
        try:
            self.lease.seek(0)
            if not self.lease.read(1):
                self.lease.write(b"0")
                self.lease.flush()
            self.lease.seek(0)
            import msvcrt
            msvcrt.locking(self.lease.fileno(),msvcrt.LK_NBLCK,1)
        except OSError as exc:
            self.lease.close()
            raise AnalysisError("Приложение уже запущено в другом терминале") from exc
        self.queue = WorkspaceQueue(self.registry, python)
        self.picker_lock = threading.Lock()

    def editor(self, project_id):
        with self.queue.lock:
            project = self.registry.get(project_id)
            analysis = project.folder("analysis")
            stamp = tuple((analysis/n).stat().st_mtime_ns for n in ("segments.jsonl","media-index.json"))
            previous = self.editors.get(project_id)
            if not previous or previous[0] != stamp:
                store = ProjectEditorStore(project)
                app = EditorApp(None,store=store,jobs=ProjectQueue(store,self.queue),
                                url_prefix="/p/"+project_id,baselines_root=project.folder("renders"),proxy_options=self.proxy_options)
                app.token = self.token
                if previous:
                    app.assets.update(previous[1].assets)
                self.editors[project_id] = (stamp,app)
            return self.editors[project_id][1]

    def jobs(self):
        with self.queue.lock:
            jobs = self.queue.list()
            for job in jobs:
                if job.get("project_id"):
                    job["project_name"] = self.registry.get(job["project_id"]).read()["name"]
            return jobs

    def delete_project(self, project_id):
        with self.queue.lock:
            jobs = self.queue.list(project_id)
            if any(job["status"] == "running" or job["id"] == self.queue.active for job in jobs):
                raise Conflict("Поставьте задания проекта на паузу и дождитесь остановки перед удалением.")
            value = self.registry.delete(project_id)
            previous = self.editors.pop(project_id, None)
            if previous:
                previous[1].close()
            return value

    def pick_folder(self):
        if not self.picker_lock.acquire(blocking=False):
            raise AnalysisError("Диалог выбора папки уже открыт")
        try:
            code = """Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Выберите папку'
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::WriteLine($dialog.SelectedPath) }
$dialog.Dispose()
"""
            encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(["powershell","-NoProfile","-STA","-WindowStyle","Hidden","-EncodedCommand",encoded],
                                    capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise AnalysisError(result.stderr.decode("utf-8","replace"))
            return {"path":result.stdout.decode("utf-8-sig","replace").strip()}
        finally:
            self.picker_lock.release()

    def close(self):
        try:
            self.queue.close()
        finally:
            self.lease.close()


class WorkspaceServer(EditorHTTPServer):
    def __init__(self,address,app):
        from http.server import ThreadingHTTPServer
        ThreadingHTTPServer.__init__(self,address,WorkspaceHandler)
        self.app = app


class WorkspaceHandler(EditorHandler):
    @property
    def app(self):
        return getattr(self,"editor_app",self.server.app)

    def scoped_editor(self):
        match = re.fullmatch(r"/p/([a-f0-9]{32})(/.*)",urlsplit(self.path).path)
        if not match:
            return False
        self.editor_app = self.server.app.editor(match[1])
        original = urlsplit(self.path)
        self.path = match[2] + ("?"+original.query if original.query else "")
        return True

    def handle_get(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_address[1]}":
            return self.send_json({"error":"Используйте адрес 127.0.0.1"},403)
        self.editor_app = self.server.app
        route = urlsplit(self.path).path
        if self.scoped_editor():
            if urlsplit(self.path).path == "/edit":
                prefix = self.app.url_prefix
                page = (WEB/"index.html").read_text(encoding="utf-8").replace("__EDITOR_TOKEN__",html.escape(self.app.token))
                page = page.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="project-base" content="'+prefix+'">')
                page = page.replace('<body>', '<body><nav class="workspace-back"><a href="/projects/'+prefix.split('/')[-1]+'">← Проект</a> · <a href="/">Все проекты</a> · <a href="/diagnostics">Диагностика</a></nav>')
                return self.send_bytes(page.encode("utf-8"),"text/html; charset=utf-8")
            return super().handle_get()
        app = self.server.app
        if route in ("/","/diagnostics") or re.fullmatch(r"/projects/[a-f0-9]{32}",route):
            page = (WEB/"workspace.html").read_text(encoding="utf-8").replace("__EDITOR_TOKEN__",html.escape(app.token))
            return self.send_bytes(page.encode("utf-8"),"text/html; charset=utf-8")
        if route in ("/workspace.js","/workspace.css","/app.js","/app.css"):
            path = WEB/route[1:]
            return self.send_bytes(path.read_bytes(),"text/javascript; charset=utf-8" if path.suffix==".js" else "text/css; charset=utf-8")
        if route == "/api/projects":
            return self.send_json({"projects":app.registry.list()})
        if route == "/api/jobs":
            return self.send_json(app.jobs())
        if route == "/api/diagnostics":
            path = app.registry.root/"diagnostics/diagnostics.json"
            return self.send_json(load_json(path) if path.exists() else {"checks":[],"ready":False})
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/log",route)
        if match:
            path = app.queue.path(match[1]).parent/"worker.log"
            if not path.exists():
                return self.send_json({"text":"Журнал пока пуст"})
            with path.open("rb") as stream:
                stream.seek(max(0,path.stat().st_size-65536))
                return self.send_json({"text":stream.read().decode("utf-8","replace")})
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(/.*)?",route)
        if match:
            project = app.registry.get(match[1])
            action = match[2] or ""
            if not action:
                return self.send_json(project.payload())
            if action == "/catalog":
                data = catalog(project)
                return self.send_json({"revision":data["revision"],"visual":data["visual"],"speech":data["speech"]})
            if action == "/handoff.md":
                path,text,_ = export_markdown(project)
                data = text.encode("utf-8")
                self.headers_for(200,len(data),"text/markdown; charset=utf-8",{"Content-Disposition":'attachment; filename="ai-instructions.md"'})
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
        return self.send_json({"error":"Не найдено"},404)

    def do_POST(self):
        self.editor_app = self.server.app
        try:
            if self.scoped_editor():
                return super().do_POST()
            app = self.server.app
            expected = f"127.0.0.1:{self.server.server_address[1]}"
            if (self.headers.get("Host") != expected or self.headers.get("X-Editor-Token") != app.token
                    or self.headers.get("Origin") not in (None,"http://"+expected)):
                self.close_connection = True
                return self.send_json({"error":"Запрос не из локального интерфейса"},403)
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 2*1024*1024:
                self.close_connection = True
                return self.send_json({"error":"Допустим JSON до 2 МБ"},413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise AnalysisError("Ожидался объект JSON")
            route = urlsplit(self.path).path
            with app.queue.lock:
                if route == "/api/projects":
                    value = app.registry.create(body.get("name"),body.get("source"),body.get("workspace")).payload()
                elif route == "/api/projects/open":
                    value = app.registry.open(body["path"]).payload()
                elif route == "/api/folders":
                    # The native dialog must not hold the processing queue's lock.
                    value = None
                elif route == "/api/diagnostics/check":
                    value = app.queue.enqueue("diagnostics",output=str(app.registry.root/"diagnostics"))
                elif route == "/api/install":
                    components = body.get("components")
                    if not isinstance(components,list) or not components or any(c not in INSTALLABLE for c in components):
                        raise AnalysisError("Неизвестный компонент установки")
                    value = app.queue.enqueue("install",components=list(dict.fromkeys(components)),output=str(app.registry.root/"diagnostics"))
                elif route in ("/api/pause","/api/resume"):
                    value = (app.queue.pause if route.endswith("pause") else app.queue.resume)(body["id"])
                else:
                    match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/(.*)",route)
                    if not match:
                        return self.send_json({"error":"Не найдено"},404)
                    action = match[2]
                    if action == "delete":
                        value = app.delete_project(match[1])
                        self.send_json(value)
                        return
                    project = app.registry.get(match[1])
                    if action == "brief":
                        value = project.update_brief(body["brief"])
                    elif action == "source-selection":
                        if any(j["kind"]=="analyze" and j["status"] in ("queued","running") for j in app.queue.list(project.id)):
                            raise AnalysisError("Поставьте анализ на паузу перед изменением списка исходников.")
                        value = project.select_source(body.get("source_id"), body.get("excluded"))
                        if not body.get("excluded"):
                            app.queue.enqueue("scan", project, excluded_sources=project.read().get("excluded_sources", []))
                    elif action in ("scan","analyze"):
                        parameters = {"settings":project.read()["analysis_settings"]} if action=="analyze" else {}
                        value = app.queue.enqueue(action,project,**parameters)
                    elif action == "handoff":
                        path,text,revision = export_markdown(project)
                        value = {"url":f"/api/projects/{project.id}/handoff.md","size_bytes":len(text.encode("utf-8")),"catalog_revision":revision}
                    elif action == "scenarios/validate":
                        value = validate_bundle(project,body["bundle"])
                    elif action == "scenarios/import":
                        value = import_bundle(project,body["bundle"])
                    else:
                        return self.send_json({"error":"Не найдено"},404)
            if route == "/api/folders":
                value = app.pick_folder()
            self.send_json(value)
        except Conflict as exc:
            self.send_json({"error":str(exc)},409)
        except (AnalysisError,OSError,ValueError,KeyError,TypeError) as exc:
            self.send_json({"error":str(exc)},400)


def serve(port):
    app = WorkspaceApp()
    try:
        server = WorkspaceServer(("127.0.0.1",port),app)
    except BaseException:
        app.close()
        raise
    app.queue.start()
    print(f"AI Video: http://127.0.0.1:{server.server_address[1]}\nCtrl+C: остановить, сохранив задания",flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
