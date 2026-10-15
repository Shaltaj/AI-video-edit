import copy
import json
from pathlib import Path
import threading
import time

import pytest
import requests

from aivideo import render
from aivideo.common import AnalysisError, atomic_json, load_json
from aivideo.editor import EditorApp, EditorHTTPServer
from aivideo.editor_jobs import JobQueue
from aivideo.editor_store import Conflict, EditorStore

local_http = requests.Session()
local_http.trust_env = False


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError("Timed out waiting for local operation")


def test_v2_titles_cross_cuts_and_only_changed_intersections_invalidate(edit_assets, tmp_path):
    path, original = edit_assets
    v2 = render.upgrade_plan(original)
    assert original["schema_version"] == 1 and "title" in original["shots"][0]
    cache = tmp_path / "cache"
    first = render.render(path, tmp_path / "first", cache=cache)
    atomic_json(path, v2)
    migrated = render.render(path, tmp_path / "migrated", cache=cache)
    assert all(s["cache_hit"] for s in migrated["shots"])
    v2["titles"][0].update(start_s=0.6, end_s=1.4)
    atomic_json(path, v2)
    resolved = render.load_plan(path)
    assert [(o["start_s"], o["end_s"]) for s in resolved["shots"] for o in s["overlays"]] == [(0.6, 1.0), (0.0, 0.4)]
    crossed = render.render(path, tmp_path / "crossed", cache=cache)
    assert [s["cache_hit"] for s in crossed["shots"]] == [False, False, True]
    v2["titles"][0]["start_s"] = 0.7
    atomic_json(path, v2)
    changed = render.render(path, tmp_path / "changed", cache=cache)
    assert [s["cache_hit"] for s in changed["shots"]] == [False, True, True]
    v2["shots"][0], v2["shots"][1] = v2["shots"][1], v2["shots"][0]
    atomic_json(path, v2)
    assert render.load_plan(path)["titles"][0]["start_s"] == 0.7


def test_store_migration_history_conflict_variants_and_title_errors(edit_assets, tmp_path):
    path, original = edit_assets
    store = EditorStore(path, tmp_path / "state")
    initial = store.get(store.default_id)
    assert initial["plan"]["schema_version"] == 2 and load_json(path)["schema_version"] == 1
    saved = store.save(store.default_id, initial["plan"], initial["revision"])
    assert load_json(path)["schema_version"] == 2
    assert (store.root / "backups" / f"{store.default_id}-v1.json").exists()
    other = store.create(store.default_id, "Другой вариант", saved["plan"])
    assert other["variant"] != store.default_id
    reopened = EditorStore(Path(other["path"]), tmp_path / "state")
    assert reopened.default_id == other["variant"] and len(reopened.variants()) == 2
    external = load_json(path)
    external["title"] = "Правка через Codex"
    atomic_json(path, external)
    with pytest.raises(Conflict):
        store.save(store.default_id, saved["plan"], saved["revision"])
    current = store.get(store.default_id)
    assert current["plan"]["title"] == "Правка через Codex"
    restored = store.restore(store.default_id, initial["revision"], current["revision"])
    assert restored["plan"]["title"] == original["title"]
    out_of_bounds = copy.deepcopy(restored["plan"])
    out_of_bounds["titles"][0]["end_s"] = 4
    invalid = store.save(store.default_id, out_of_bounds, restored["revision"])
    assert invalid["errors"]
    with pytest.raises(AnalysisError, match="титр"):
        store.snapshot(store.default_id, tmp_path / "snapshot.json", invalid["revision"])
    overlap = copy.deepcopy(restored["plan"])
    overlap["titles"].append({"id": "second", "text": "Два", "start_s": 0.5, "end_s": 1.5})
    with pytest.raises(AnalysisError, match="пересекаться"):
        store.save(store.default_id, overlap, invalid["revision"])
    again = EditorStore(path, tmp_path / "state")
    assert len(again.variants()) == 2 and again.get(store.default_id)["errors"]


@pytest.fixture
def local_editor(edit_assets, tmp_path):
    path, _ = edit_assets
    app = EditorApp(path, state_root=tmp_path / "editor", cache=tmp_path / "cache",
                    proxy_options={"decode": "cpu", "encoder": "libx264"})
    server = EditorHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.jobs.start()
    address = f"http://127.0.0.1:{server.server_address[1]}"
    yield app, address
    server.shutdown()
    app.close()
    server.server_close()
    thread.join()


def test_api_ranges_registered_files_and_conflicts(local_editor, tmp_path):
    app, address = local_editor
    file = tmp_path / "Медиа с пробелами.mp4"
    file.write_bytes(bytes(range(100)))
    url = app.asset(file)
    r = local_http.get(address + url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == bytes(range(10, 20))
    assert r.headers["Content-Range"] == "bytes 10-19/100"
    r = local_http.head(address + url, headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.headers["Content-Length"] == "5" and not r.content
    assert local_http.get(address + url, headers={"Range": "bytes=100-101"}).status_code == 416
    assert local_http.get(address + "/media/unknown").status_code == 404
    assert local_http.get(address + "/api/project", headers={"Host": "foreign.example"}).status_code == 403
    assert local_http.post(address + "/api/render", json={}).status_code == 403
    headers = {"X-Editor-Token": app.token}
    value = local_http.get(address + "/api/plan").json()
    bad = local_http.post(address + "/api/plan", json={**value, "revision": "wrong"}, headers=headers)
    assert bad.status_code == 409
    current = local_http.post(address + "/api/plan", json=value, headers=headers)
    assert current.status_code == 200 and current.json()["plan"]["schema_version"] == 2
    cat = local_http.get(address + "/api/catalog").json()
    assert len(cat["entries"]) == 3


def test_durable_snapshot_manual_restart_proxy_and_resume(edit_assets, tmp_path):
    path, _ = edit_assets
    store = EditorStore(path, tmp_path / "state")
    queue = JobQueue(store, cache=tmp_path / "cache")
    value = store.get(store.default_id)
    job = queue.add_render(store.default_id, value["revision"])
    assert queue.add_render(store.default_id, value["revision"])["id"] == job["id"]
    snapshot = Path(job["snapshot"]).read_bytes()
    value["plan"]["shots"][0]["audio"] = {"mode": "original", "gain": 0.6}
    store.save(store.default_id, value["plan"], value["revision"])
    assert Path(job["snapshot"]).read_bytes() == snapshot
    queue = JobQueue(store, cache=tmp_path / "cache")
    assert queue.list()[0]["status"] == "interrupted"
    queue.start()
    try:
        time.sleep(0.2)
        assert queue.list()[0]["status"] == "interrupted"
        queue.resume(job["id"])
        wait_for(lambda: load_json(queue.path(job["id"]))["status"] == "complete")
        assert (Path(job["output"]) / "film.mp4").exists()
        proxy = queue.proxy("дрон.mp4", decode="cpu", encoder="libx264")
        wait_for(lambda: load_json(queue.path(proxy["id"]))["status"] == "complete")
        metadata = render.probe(Path(proxy["target"]))
        stream = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        assert stream["codec_name"] == "h264" and stream["width"] <= 1280
        assert queue.proxy("дрон.mp4", decode="cpu", encoder="libx264")["status"] == "complete"
    finally:
        queue.close()


def test_running_render_pause_reuses_completed_fragments(edit_assets, tmp_path):
    path, spec = edit_assets
    spec = render.upgrade_plan(spec)
    spec["titles"] = []
    spec["shots"] = [{"id": f"s{i:02}", "source_id": "проезд.mp4", "start_s": (i % 10) / 30,
                       "end_s": (i % 10) / 30 + 1} for i in range(20)]
    atomic_json(path, spec)
    store = EditorStore(path, tmp_path / "state")
    queue = JobQueue(store, cache=tmp_path / "cache")
    job = queue.add_render(store.default_id, store.get(store.default_id)["revision"])
    queue.start()
    try:
        progress = Path(job["output"]) / "render.json"
        wait_for(lambda: progress.exists() and len(load_json(progress).get("shots", [])) >= 1)
        queue.pause(job["id"])
        wait_for(lambda: load_json(queue.path(job["id"]))["status"] == "paused")
        done = len(load_json(progress)["shots"])
        assert done >= 1
        queue.resume(job["id"])
        wait_for(lambda: load_json(queue.path(job["id"]))["status"] == "complete")
        final = load_json(progress)
        assert sum(s["cache_hit"] for s in final["shots"]) >= done
        assert final["validation"]["frames"] == 600
    finally:
        queue.close()


def test_browser_edits_variants_and_background_render(local_editor):
    from playwright.sync_api import sync_playwright, expect

    app, address = local_editor
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(address)
            expect(page.locator(".scene-card")).to_have_count(3)
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            page.locator("#shot-end").fill("1.7")
            page.locator("#shot-end").dispatch_event("change")
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            expect(page.locator("#shot-end")).to_have_value("1.7")
            page.locator("#new-variant").click()
            page.locator("#variant-name").fill("Проверка браузера")
            page.locator("#create-variant").click()
            expect(page.locator("#variant-dialog")).not_to_be_visible()
            expect(page.locator("#variants option")).to_have_count(2)
            page.locator("#kind").select_option("image")
            expect(page.locator(".catalog-card")).to_have_count(1)
            page.locator(".catalog-card").get_by_role("button", name="Добавить", exact=True).click()
            expect(page.locator(".scene-card")).to_have_count(4)
            expect(page.locator("#save-status")).to_have_text("Все правки сохранены")
            page.reload()
            expect(page.locator(".scene-card")).to_have_count(4)
            page.locator("#render").click()
            wait_for(lambda: any(j["kind"] == "render" for j in app.jobs.list()))
            page.close()
            wait_for(lambda: any(j["kind"] == "render" and j["status"] == "complete" for j in app.jobs.list()))
            assert not errors
        finally:
            browser.close()
