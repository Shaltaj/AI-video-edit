"""Project manifests and isolated workspaces. Standard library only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from .common import AnalysisError, ROOT, TOOLS, atomic_json, digest, load_json, now
from .editor_store import EditorStore, Conflict

MANIFEST = "project.aivideo.json"
FOLDERS = ("analysis", "plans", "editor", "jobs", "cache", "renders", "exports", "tmp")
DEFAULT_ANALYSIS = {"window_s": 10.0, "fps": 2.0, "max_side": 512, "decode": "cuda"}


class Project:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.root = self.path.parent
        self.lock = threading.RLock()
        value = self.read()
        self.id = value["id"]

    def read(self):
        value = load_json(self.path)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("id", "")))
                or not isinstance(value.get("name"), str) or not value["name"].strip()
                or not isinstance(value.get("brief"), str) or not isinstance(value.get("analysis_settings"), dict)
                or not isinstance(value.get("variants"), dict) or not isinstance(value.get("source_root"), str)
                or not Path(value["source_root"]).is_absolute()):
            raise AnalysisError("Некорректный файл проекта")
        return value

    @property
    def source(self):
        return Path(self.read()["source_root"]).resolve()

    def folder(self, name):
        if name not in FOLDERS or not self.root.is_dir():
            raise AnalysisError(f"Рабочая папка недоступна: {self.root}")
        path = (self.root / name).resolve()
        if self.root not in path.parents:
            raise AnalysisError("Папка проекта выходит за рабочий каталог")
        return path

    def update_brief(self, brief):
        if not isinstance(brief, str) or len(brief) > 100_000:
            raise AnalysisError("Задание должно быть текстом до 100000 символов")
        with self.lock:
            value = self.read()
            value.update(brief=brief, updated_at=now())
            atomic_json(self.path, value)
        return value

    def select_source(self, source_id, excluded):
        if not isinstance(source_id, str) or type(excluded) is not bool:
            raise AnalysisError("Нужны source_id и признак excluded")
        with self.lock:
            value = self.read()
            selection = set(value.get("excluded_sources", []))
            index_path = self.folder("analysis") / "media-index.json"
            index = load_json(index_path) if index_path.exists() else {}
            known = {m["id"] for m in index.get("media", [])} | selection
            known.update(e["source_id"] for e in index.get("errors", []))
            known.update(e["source_id"] for e in index.get("excluded", []))
            if source_id not in known:
                raise AnalysisError("Файл отсутствует в списке исходников. Сначала прочитайте список.")
            path = (self.source / source_id).resolve()
            if self.source not in path.parents:
                raise AnalysisError("Путь выходит за папку исходников")
            selection.add(source_id) if excluded else selection.discard(source_id)
            value.update(excluded_sources=sorted(selection), updated_at=now())
            atomic_json(self.path, value)
        return self.payload()

    def payload(self):
        value = self.read()
        result = {**value, "workspace": str(self.root), "manifest": str(self.path),
                  "source_available": self.source.is_dir(), "analysis_status": "not_started"}
        run = self.folder("analysis") / "run.json"
        if run.exists():
            state = load_json(run)
            result.update(analysis_status=state["status"], analysis_errors=state.get("errors", []))
            result["skipped_sources"] = state.get("skipped_sources", [])
            if state["status"] == "complete" and sorted(state.get("excluded_sources", [])) != sorted(value.get("excluded_sources", [])):
                result["analysis_status"] = "stale"
        index = self.folder("analysis") / "media-index.json"
        if index.exists():
            data = load_json(index)
            excluded = set(value.get("excluded_sources", []))
            data["media"] = [m for m in data["media"] if m["id"] not in excluded]
            data["errors"] = [e for e in data["errors"] if e["source_id"] not in excluded]
            # A restored file is read again before analysis; its previous skip is no longer an exclusion.
            data["excluded"] = [{"source_id": name} for name in sorted(excluded)]
            result["index"] = data
        return result


class ProjectRegistry:
    def __init__(self, state_root: Path | None = None, app_root: Path = ROOT):
        self.root = (state_root or TOOLS / "app").resolve()
        self.app_root = app_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "projects.json"
        self.lock = threading.RLock()
        self.projects = {}
        self.recent = load_json(self.path) if self.path.exists() else {}
        for key, entry in self.recent.items():
            try:
                project = Project(Path(entry["manifest"]))
                if project.id == key:
                    self.projects[key] = project
            except (OSError, ValueError, AnalysisError):
                pass

    def get(self, key):
        with self.lock:
            if key not in self.projects:
                raise AnalysisError("Проект недоступен. Откройте его рабочую папку ещё раз.")
            return self.projects[key]

    def register(self, project):
        with self.lock:
            previous = self.projects.get(project.id)
            if previous and previous.path != project.path:
                raise AnalysisError("Проект с таким идентификатором уже открыт из другой папки")
            self.projects.setdefault(project.id, project)
            value = project.read()
            self.recent[project.id] = {"manifest": str(project.path), "name": value["name"], "opened_at": now()}
            atomic_json(self.path, self.recent)
            return self.projects[project.id]

    def open(self, path):
        candidate = Path(path).resolve()
        if candidate.is_dir():
            candidate /= MANIFEST
        return self.register(Project(candidate))

    def create(self, name, source, workspace):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise AnalysisError("Название проекта: от 1 до 120 символов")
        if not source or not workspace:
            raise AnalysisError("Укажите папку исходников и рабочую папку")
        source, workspace = Path(source).resolve(), Path(workspace).resolve()
        if not source.is_dir():
            raise AnalysisError(f"Папка исходников недоступна: {source}")
        if workspace == self.app_root or self.app_root in workspace.parents:
            raise AnalysisError("Рабочая папка должна находиться вне папки приложения")
        if workspace == source or source in workspace.parents or workspace in source.parents:
            raise AnalysisError("Рабочая папка и исходники должны быть отдельными каталогами")
        if (workspace / MANIFEST).exists():
            raise AnalysisError("В этой папке уже есть проект. Используйте «Открыть проект».")
        if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
            raise AnalysisError("Для нового проекта выберите пустую рабочую папку")
        workspace.mkdir(parents=True, exist_ok=True)
        for folder in FOLDERS:
            (workspace / folder).mkdir()
        atomic_json(workspace / MANIFEST, {"schema_version": 1, "id": uuid.uuid4().hex,
                    "name": name.strip(), "source_root": str(source), "created_at": now(), "brief": "",
                    "analysis_settings": dict(DEFAULT_ANALYSIS), "excluded_sources": [], "variants": {}})
        return self.open(workspace)

    def list(self):
        with self.lock:
            result = []
            for key, entry in self.recent.items():
                available = key in self.projects and self.projects[key].path.is_file()
                result.append({"id": key, **entry, "workspace": str(Path(entry["manifest"]).parent),
                               "available": available})
            return sorted(result, key=lambda v: v["opened_at"], reverse=True)

    def delete(self, key):
        with self.lock:
            if key not in self.recent:
                raise AnalysisError("Проект отсутствует в списке")
            manifest = Path(self.recent[key]["manifest"])
            if not manifest.is_absolute() or manifest.name != MANIFEST:
                raise AnalysisError("Не удалось определить рабочую папку проекта")
            workspace = manifest.parent
            if workspace.parent == workspace:
                raise AnalysisError("Нельзя удалить корень диска")
            # Some Windows drives support file I/O but cannot resolve a strict final path.
            # Check links explicitly, including ancestors, before using the normal resolver.
            if any(path.is_symlink() or path.is_junction() for path in (manifest, *manifest.parents)):
                raise AnalysisError("Рабочая папка и файл проекта не должны быть ссылками")
            if workspace.resolve() != workspace:
                raise AnalysisError("Рабочая папка не должна быть ссылкой")
            if workspace.exists():
                project = self.projects.get(key) or Project(manifest)
                with project.lock:
                    if manifest.resolve() != manifest:
                        raise AnalysisError("Рабочая папка и файл проекта не должны быть ссылками")
                    value = project.read()
                    if value["id"] != key or project.path != manifest:
                        raise AnalysisError("Рабочая папка относится к другому проекту")
                    protected = [self.app_root, self.root, project.source]
                    for other_key, entry in self.recent.items():
                        if other_key != key:
                            protected.append(Path(entry["manifest"]).resolve().parent)
                    for other_key, other in self.projects.items():
                        if other_key != key and other.path.is_file():
                            protected.append(other.source)
                    for path in protected:
                        if workspace == path or workspace in path.parents or path in workspace.parents:
                            raise AnalysisError("Рабочая папка пересекается с исходниками, приложением или другим проектом")
                    def walk_error(error):
                        # Some Windows filesystems report an empty scandir search as a missing path.
                        # Accept it only after an independent listing proves the directory is empty.
                        if isinstance(error, FileNotFoundError) and error.filename:
                            failed = Path(error.filename)
                            if (failed.resolve().is_relative_to(workspace) and failed.is_dir()
                                    and not failed.is_symlink() and not failed.is_junction()
                                    and not any(failed.iterdir())):
                                return
                        raise error
                    def remove_error(function, _path, error):
                        if function is os.scandir:
                            walk_error(error)
                        else:
                            raise error
                    for folder, directories, files in os.walk(workspace, followlinks=False, onerror=walk_error):
                        for name in directories + files:
                            child = Path(folder) / name
                            if child.is_symlink() or child.is_junction():
                                raise AnalysisError("В рабочей папке есть ссылки. Уберите их перед удалением проекта.")
                    # Keep the manifest until all other data is gone, so failed deletions can be retried.
                    for child in workspace.iterdir():
                        if child == manifest:
                            continue
                        if child.is_dir():
                            shutil.rmtree(child, onexc=remove_error)
                        else:
                            child.unlink()
                    manifest.unlink()
                    try:
                        workspace.rmdir()
                    except OSError:
                        atomic_json(manifest, value)
                        raise
            elif not Path(workspace.anchor).is_dir():
                raise AnalysisError("Диск с рабочей папкой недоступен. Подключите его перед удалением проекта.")
            recent = {project_id: entry for project_id, entry in self.recent.items() if project_id != key}
            atomic_json(self.path, recent)
            self.recent = recent
            self.projects.pop(key, None)
            return {"id": key, "deleted": True}


class ProjectEditorStore(EditorStore):
    """Reuse editor validation/history with a project-owned variant registry."""
    def __init__(self, project: Project):
        self.project = project
        self.analysis, self.source = project.folder("analysis"), project.source
        self.root = project.folder("editor")
        self.root.mkdir(exist_ok=True)
        self.lock = project.lock
        self.registry_path = project.path
        self.registry = project.read()
        if not self.registry["variants"]:
            raise AnalysisError("Сначала импортируйте сценарий")
        self.default_id = next(iter(self.registry["variants"]))
        self.index = load_json(self.analysis / "media-index.json")
        self.library = {m["id"]: m for m in self.index["media"]}
        self.visual = [json.loads(line) for line in (self.analysis / "segments.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

    def path(self, variant):
        self.registry = self.project.read()
        entry = self.registry["variants"].get(variant)
        if not entry:
            raise AnalysisError("Неизвестный вариант")
        path = (self.project.root / entry["path"]).resolve()
        if self.project.folder("plans") not in path.parents:
            raise AnalysisError("План выходит за папку сценариев проекта")
        return path

    def variants(self):
        self.registry = self.project.read()
        return [{"id": key, **value} for key, value in self.registry["variants"].items()]

    def create(self, parent, name, spec):
        with self.lock:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise AnalysisError("Имя варианта: от 1 до 120 символов")
            from .render import upgrade_plan
            spec = upgrade_plan(spec)
            spec["analysis"] = "../analysis"
            variant = uuid.uuid4().hex[:16]
            path = self.project.folder("plans") / f"{variant}.json"
            self.validate(spec, path)
            atomic_json(path, spec)
            value = self.project.read()
            value["variants"][variant] = {"name": name.strip(), "path": f"plans/{variant}.json"}
            atomic_json(self.project.path, value)
            return self.get(variant)

    def snapshot(self, variant, destination, expected):
        with self.lock:
            value = self.get(variant)
            if value["revision"] != expected:
                raise Conflict("План изменился перед запуском обработки")
            if value["errors"]:
                raise AnalysisError("; ".join(value["errors"]))
            atomic_json(destination, {**value["plan"], "analysis": os.path.relpath(self.analysis, destination.parent).replace("\\", "/")})
            return value
