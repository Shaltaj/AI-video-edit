from collections import Counter
import os
from pathlib import Path
import subprocess

import pytest
from playwright.sync_api import sync_playwright, expect

from aivideo import projects
from aivideo.common import AnalysisError, atomic_json, load_json
from aivideo.editor_store import Conflict
from aivideo.handoff import catalog, example_bundle, import_bundle
from aivideo.job_paths import write_job
from aivideo.projects import FOLDERS, MANIFEST, ProjectRegistry
from test_editor import local_http
from test_workspace import managed_project, workspace_server


@pytest.fixture
def registry_project(tmp_path):
    source = tmp_path / "Исходники"
    source.mkdir()
    (source / "original.mp4").write_bytes(b"original")
    registry = ProjectRegistry(tmp_path / "state", tmp_path / "application")
    project = registry.create("Первый фильм", source, tmp_path / "Рабочая папка")
    return registry, project


def test_delete_workspace_and_persist_registry_without_touching_sources(registry_project, tmp_path):
    registry, project = registry_project
    source = project.source
    second = registry.create("Второй фильм", source, tmp_path / "Другой проект")
    for folder in FOLDERS:
        (project.folder(folder) / "artifact.txt").write_text("data", encoding="utf-8")
    (project.root / "notes.txt").write_text("notes", encoding="utf-8")
    assert registry.delete(project.id) == {"id": project.id, "deleted": True}
    assert not project.root.exists()
    assert (source / "original.mp4").read_bytes() == b"original"
    assert second.path.is_file()
    assert [entry["id"] for entry in registry.list()] == [second.id]
    assert registry.list()[0]["workspace"] == str(second.root)
    reopened = ProjectRegistry(registry.root, registry.app_root)
    assert list(reopened.projects) == [second.id]
    assert project.id not in load_json(registry.path)
    with pytest.raises(AnalysisError):
        registry.get(project.id)


@pytest.mark.parametrize("layout", ["empty", "nested", "partial"])
def test_delete_all_folders_when_empty_scandir_raises_not_found(registry_project, monkeypatch, layout):
    registry, project = registry_project
    source = project.source
    if layout == "nested":
        for folder in FOLDERS:
            (project.folder(folder) / "artifact.txt").write_bytes(b"data")
            (project.folder(folder) / "nested" / "empty").mkdir(parents=True)
    elif layout == "partial":
        for folder in FOLDERS[:3]:
            project.folder(folder).rmdir()
    (project.root / "notes.txt").write_bytes(b"notes")
    scandir = os.scandir
    failed_scans = Counter()

    def windows_empty_scandir(path):
        if not isinstance(path, int):
            directory = Path(path)
            if project.root in directory.parents and directory.is_dir() and not os.listdir(directory):
                failed_scans[directory] += 1
                raise FileNotFoundError(2, "Empty directory has no search results", os.fspath(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", windows_empty_scandir)
    registry.delete(project.id)
    assert not project.root.exists() and registry.list() == []
    assert (source / "original.mp4").read_bytes() == b"original"
    expected_folders = FOLDERS[3:] if layout == "partial" else FOLDERS
    for folder in expected_folders:
        empty = project.root / folder
        if layout == "nested":
            empty /= "nested/empty"
        # Cover both preflight traversal and shutil's recursive deletion.
        assert failed_scans[empty] >= 2


@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
def test_unreadable_nonempty_folder_is_not_treated_as_empty(registry_project, monkeypatch, error_type):
    registry, project = registry_project
    folder = project.folder("cache")
    artifact = folder / "keep.txt"
    artifact.write_bytes(b"keep")
    scandir = os.scandir

    def unreadable(path):
        if not isinstance(path, int) and Path(path) == folder:
            raise error_type(2, "Cannot enumerate directory", os.fspath(path))
        return scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    with pytest.raises(error_type):
        registry.delete(project.id)
    assert artifact.read_bytes() == b"keep" and project.path.is_file()
    assert project.id in load_json(registry.path)


@pytest.mark.parametrize("protected", ["source", "app", "state", "other_workspace", "other_source", "identity", "drive_root"])
def test_delete_rejects_protected_or_replaced_workspaces(registry_project, tmp_path, protected):
    registry, project = registry_project
    source = project.source
    if protected in ("source", "identity"):
        value = project.read()
        value["source_root" if protected == "source" else "id"] = str(project.root) if protected == "source" else "0" * 32
        atomic_json(project.path, value)
    elif protected == "app":
        registry.app_root = project.root / "application"
    elif protected == "state":
        registry.root = project.root / "state"
    elif protected == "other_workspace":
        registry.create("Вложенный проект", source, project.root / "nested")
    elif protected == "other_source":
        registry.create("Использует файлы первого", project.folder("renders"), tmp_path / "other")
    elif protected == "drive_root":
        registry.recent[project.id]["manifest"] = str(Path(project.root.anchor) / MANIFEST)
    with pytest.raises(AnalysisError):
        registry.delete(project.id)
    assert project.path.is_file()
    assert (source / "original.mp4").read_bytes() == b"original"
    assert project.id in registry.recent


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_delete_rejects_junctions_before_removing_any_data(registry_project, tmp_path):
    registry, project = registry_project
    outside = tmp_path / "Отдельные данные"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    junction = project.folder("cache") / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(AnalysisError, match="ссылки"):
            registry.delete(project.id)
        assert all(project.folder(folder).is_dir() for folder in FOLDERS)
        assert (outside / "keep.txt").read_bytes() == b"keep"
        assert project.path.is_file()
    finally:
        assert junction.is_junction() and project.root in junction.parents
        junction.rmdir()


@pytest.mark.parametrize("failure", ["contents", "workspace"])
def test_partial_delete_keeps_manifest_and_registry_for_retry(registry_project, monkeypatch, failure):
    registry, project = registry_project
    source = project.source
    with monkeypatch.context() as patch:
        if failure == "contents":
            def denied(*_, **__):
                raise PermissionError("File is in use")
            patch.setattr(projects.shutil, "rmtree", denied)
        else:
            original = Path.rmdir
            def denied(path):
                if path == project.root:
                    raise PermissionError("Folder is in use")
                return original(path)
            patch.setattr(Path, "rmdir", denied)
        with pytest.raises(PermissionError):
            registry.delete(project.id)
    assert project.read()["id"] == project.id
    assert project.id in load_json(registry.path)
    registry.delete(project.id)
    assert not project.root.exists()
    assert (source / "original.mp4").read_bytes() == b"original"


def test_registry_write_failure_can_be_retried_after_workspace_is_gone(registry_project, monkeypatch):
    registry, project = registry_project
    with monkeypatch.context() as patch:
        def denied(*_, **__):
            raise PermissionError("Registry is locked")
        patch.setattr(projects, "atomic_json", denied)
        with pytest.raises(PermissionError):
            registry.delete(project.id)
    assert not project.root.exists() and project.id in registry.recent
    registry.delete(project.id)
    assert registry.list() == [] and load_json(registry.path) == {}


def test_offline_drive_keeps_recent_project(registry_project, monkeypatch):
    registry, project = registry_project
    exists, is_dir = Path.exists, Path.is_dir
    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", lambda path: False if path == project.root else exists(path))
        patch.setattr(Path, "is_dir", lambda path: False if path == Path(project.root.anchor) else is_dir(path))
        with pytest.raises(AnalysisError, match="Диск"):
            registry.delete(project.id)
    assert project.path.is_file() and project.id in registry.recent


def test_delete_api_authentication_queue_cleanup_and_stale_editor(workspace_server, tmp_path):
    app, address, key = workspace_server
    app.queue.stop.set()
    app.queue.thread.join(timeout=5)
    assert not app.queue.thread.is_alive()
    project = app.registry.get(key)
    source = project.source
    original = (source / "проезд.mp4").read_bytes()
    import_bundle(project, example_bundle(project, catalog(project)))
    editor = app.editor(key)
    second = app.registry.create("Другой проект", source, tmp_path / "other")
    app.queue.enqueue("scan", project)
    other_job = app.queue.enqueue("scan", second)
    route = address + f"/api/projects/{key}/delete"
    headers = {"X-Editor-Token": app.token}
    assert local_http.post(route, json={}).status_code == 403
    assert local_http.post(route, json={}, headers={**headers, "Origin": "http://example.com"}).status_code == 403
    assert project.path.is_file()
    response = local_http.post(route, json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"id": key, "deleted": True}
    assert not project.root.exists() and key not in app.editors
    assert [job["id"] for job in app.jobs()] == [other_job["id"]]
    assert [entry["id"] for entry in local_http.get(address + "/api/projects").json()["projects"]] == [second.id]
    assert local_http.get(address + f"/p/{key}/api/plan").status_code == 400
    with pytest.raises(AnalysisError):
        editor.jobs.proxy("проезд.mp4")
    with pytest.raises(AnalysisError):
        app.queue.enqueue("scan", project)
    assert not project.root.exists()
    assert (source / "проезд.mp4").read_bytes() == original


def test_delete_readable_workspace_when_strict_resolution_fails(workspace_server, monkeypatch):
    app, address, key = workspace_server
    project = app.registry.get(key)
    source = project.source
    original = (source / "фото.jpg").read_bytes()
    resolve = Path.resolve

    def unsupported_strict_resolve(path, strict=False):
        if strict and path in (project.root, project.path):
            raise FileNotFoundError(2, "Windows cannot resolve the final path", str(path))
        return resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", unsupported_strict_resolve)
    assert project.path.is_file() and project.read()["id"] == key
    response = local_http.post(address + f"/api/projects/{key}/delete", json={},
                               headers={"X-Editor-Token": app.token})
    assert response.status_code == 200, response.text
    assert not project.root.exists() and key not in app.registry.recent
    assert (source / "фото.jpg").read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_delete_rejects_linked_ancestor_when_resolution_falls_back(registry_project, tmp_path, monkeypatch):
    registry, project = registry_project
    junction = tmp_path / "alias"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(tmp_path)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        registry.recent[project.id]["manifest"] = str(junction / project.root.name / MANIFEST)
        registry.projects.pop(project.id)
        # Some Windows drives allow normal I/O but cannot return a canonical final path.
        monkeypatch.setattr(Path, "resolve", lambda path, strict=False: path.absolute())
        with pytest.raises(AnalysisError, match="ссыл"):
            registry.delete(project.id)
        assert project.read()["id"] == project.id
        assert all(project.folder(folder).is_dir() for folder in FOLDERS)
    finally:
        assert junction.is_junction() and junction.parent == tmp_path
        junction.rmdir()


@pytest.mark.parametrize("finishing", [False, True])
def test_delete_api_waits_for_running_worker_to_exit(workspace_server, finishing):
    app, address, key = workspace_server
    app.queue.stop.set()
    app.queue.thread.join(timeout=5)
    project = app.registry.get(key)
    job = app.queue.enqueue("scan", project)
    job["status"] = "complete" if finishing else "running"
    write_job(app.queue.path(job["id"]), job)
    app.queue.active = job["id"] if finishing else None
    headers = {"X-Editor-Token": app.token}
    route = address + f"/api/projects/{key}/delete"
    try:
        response = local_http.post(route, json={}, headers=headers)
        assert response.status_code == 409 and "паузу" in response.json()["error"]
        assert project.path.is_file()
        app.queue.active = None
        job["status"] = "paused"
        write_job(app.queue.path(job["id"]), job)
        assert local_http.post(route, json={}, headers=headers).status_code == 200
        assert not project.root.exists()
    finally:
        app.queue.active = None


def test_delete_confirmation_cancellation_and_persistence_in_edge(workspace_server, tmp_path, monkeypatch):
    app, address, key = workspace_server
    project = app.registry.get(key)
    source = project.source
    original = (source / "фото.jpg").read_bytes()
    second = app.registry.create("Сохранить этот проект", source, tmp_path / "other")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 400, "height": 1000})
        errors, messages = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(address)
            card = page.locator(f'#recent-projects [data-project="{key}"]')
            remove = card.get_by_role("button", name="Удалить проект " + project.read()["name"], exact=True)
            def cancel(dialog):
                messages.append(dialog.message)
                dialog.dismiss()
            page.once("dialog", cancel)
            remove.click()
            assert project.path.is_file()
            assert str(project.root) in messages[0] and project.read()["name"] in messages[0]
            expect(card).to_have_count(1)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            with monkeypatch.context() as patch:
                def busy(_):
                    raise Conflict("Поставьте задания проекта на паузу")
                patch.setattr(app, "delete_project", busy)
                page.once("dialog", lambda dialog: dialog.accept())
                remove.click()
                expect(page.locator("#error")).to_contain_text("паузу")
                expect(remove).to_be_enabled()
                assert project.path.is_file()
            page.once("dialog", lambda dialog: dialog.accept())
            remove.click()
            expect(card).to_have_count(0)
            assert not project.root.exists()
            expect(page.locator(f'#recent-projects [data-project="{second.id}"]')).to_have_count(1)
            page.reload()
            expect(page.locator("#recent-projects .project-card")).to_have_count(1)
            assert (source / "фото.jpg").read_bytes() == original
            assert not errors
        finally:
            browser.close()
