"""Authoritative JSON plans, optimistic concurrency and immutable history."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import uuid

from .common import AnalysisError, RUNS, atomic_json, digest, load_json, now
from .render import resolve_plan, upgrade_plan


class Conflict(AnalysisError):
    pass


class EditorStore:
    def __init__(self, plan_path: Path, state_root: Path | None = None):
        plan_path = plan_path.resolve()
        resolved = resolve_plan(load_json(plan_path), plan_path, allow_title_errors=True)
        self.analysis = Path(resolved["analysis"])
        self.source = Path(resolved["source"])
        self.root = (state_root or RUNS / "editor") / digest(str(self.analysis))[:16]
        if self.root.resolve() == self.source or self.source in self.root.resolve().parents:
            raise AnalysisError("Состояние редактора должно находиться вне исходников")
        if plan_path == self.source or self.source in plan_path.parents:
            raise AnalysisError("Монтажный план должен находиться вне исходников")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.registry_path = self.root / "project.json"
        self.registry = load_json(self.registry_path) if self.registry_path.exists() else {"variants": {}}
        self.default_id = next((key for key, entry in self.registry["variants"].items()
                                if Path(entry["path"]).resolve() == plan_path), digest(str(plan_path))[:16])
        self.registry["variants"].setdefault(self.default_id, {
            "path": str(plan_path), "name": load_json(plan_path).get("title", plan_path.stem)})
        atomic_json(self.registry_path, self.registry)
        self.index = load_json(self.analysis / "media-index.json")
        self.library = {m["id"]: m for m in self.index["media"]}
        self.visual = [json.loads(line) for line in (self.analysis / "segments.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        self.remember(self.default_id, load_json(plan_path))

    def path(self, variant: str) -> Path:
        try:
            return Path(self.registry["variants"][variant]["path"])
        except KeyError as exc:
            raise AnalysisError("Неизвестный вариант") from exc

    def remember(self, variant: str, spec: dict) -> str:
        revision = digest(spec)
        path = self.root / "history" / variant / f"{revision}.json"
        if not path.exists():
            atomic_json(path, {"saved_at": now(), "revision": revision, "plan": spec})
        return revision

    def validate(self, spec: dict, path: Path) -> dict:
        resolved = resolve_plan(spec, path, allow_title_errors=True)
        if Path(resolved["analysis"]) != self.analysis:
            raise AnalysisError("Вариант должен использовать анализ текущего проекта")
        return resolved

    def get(self, variant: str) -> dict:
        with self.lock:
            path = self.path(variant)
            raw = load_json(path)
            resolved = self.validate(raw, path)
            revision = self.remember(variant, raw)
            return {"variant": variant, "revision": revision, "path": str(path), "plan": upgrade_plan(raw),
                    "duration_s": resolved["duration_s"], "errors": resolved["validation_errors"],
                    "timeline": [{"id": s["id"], "start_s": s["timeline_start_s"],
                                  "end_s": s["timeline_end_s"]} for s in resolved["shots"]]}

    def save(self, variant: str, spec: dict, expected: str) -> dict:
        with self.lock:
            path = self.path(variant)
            raw = load_json(path)
            if digest(raw) != expected:
                raise Conflict("План изменён в другом окне или через Codex")
            spec = upgrade_plan(spec)
            self.validate(spec, path)
            self.remember(variant, raw)
            if raw.get("schema_version") in (1, 2):
                backup = self.root / "backups" / f"{variant}-v{raw['schema_version']}.json"
                if not backup.exists():
                    atomic_json(backup, {**raw, "analysis": str(self.analysis)})
            if digest(load_json(path)) != expected:
                raise Conflict("План изменён во время проверки; перечитайте файл")
            atomic_json(path, spec)
            self.remember(variant, spec)
            return self.get(variant)

    def create(self, parent: str, name: str, spec: dict) -> dict:
        with self.lock:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise AnalysisError("Имя варианта: от 1 до 120 символов")
            spec = upgrade_plan(spec)
            # Resolve analysis before moving the plan to a sibling file.
            resolved = self.validate(spec, self.path(parent))
            spec["analysis"] = resolved["analysis"]
            variant = uuid.uuid4().hex[:16]
            path = self.path(parent).parent / f"variant-{variant}.json"
            self.validate(spec, path)
            atomic_json(path, spec)
            self.registry["variants"][variant] = {"name": name.strip(), "path": str(path)}
            atomic_json(self.registry_path, self.registry)
            return self.get(variant)

    def history(self, variant: str) -> list[dict]:
        self.get(variant)
        paths = sorted((self.root / "history" / variant).glob("*.json"),
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return [{"revision": p.stem, "saved_at": load_json(p)["saved_at"]} for p in paths]

    def restore(self, variant: str, revision: str, expected: str) -> dict:
        if len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            raise AnalysisError("Неизвестная версия истории")
        record = load_json(self.root / "history" / variant / f"{revision}.json")
        return self.save(variant, record["plan"], expected)

    def variants(self) -> list[dict]:
        with self.lock:
            return [{"id": key, **value} for key, value in self.registry["variants"].items()]

    def snapshot(self, variant: str, destination: Path, expected: str) -> dict:
        with self.lock:
            value = self.get(variant)
            if value["revision"] != expected:
                raise Conflict("План изменился перед запуском сборки")
            if value["errors"]:
                raise AnalysisError("; ".join(value["errors"]))
            spec = {**value["plan"], "analysis": str(self.analysis)}
            atomic_json(destination, spec)
            return value


def movie_digest(spec: dict, path: Path) -> str:
    """Ignores editorial prose and path spelling; includes all rendered content."""
    spec = upgrade_plan(spec)
    content = {"analysis": str((path.parent / spec["analysis"]).resolve()),
               "settings": spec.get("settings", {}), "shots": [
                   {k: v for k, v in shot.items() if k != "purpose"} for shot in spec["shots"]],
               "titles": spec.get("titles", [])}
    if spec.get("transitions"):
        content["transitions"] = spec["transitions"]
    return digest(content)
