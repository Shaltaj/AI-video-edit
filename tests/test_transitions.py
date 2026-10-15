from pathlib import Path

import numpy as np
import pytest

from aivideo import render
from aivideo.common import AnalysisError, atomic_json, command, executable, load_json, tool_version
from aivideo.editor_jobs import JobQueue
from aivideo.editor_store import EditorStore
from aivideo.transitions import preview_spec, render_preview, render_section, sections
from test_editor import wait_for, local_editor, local_http


def transition(a="s01", b="s02", kind="crossfade", duration=.2):
    return {"from_shot_id": a, "to_shot_id": b, "type": kind, "duration_s": duration}


def modern(assets):
    path, original = assets
    spec = render.upgrade_plan(original)
    spec["titles"] = []
    spec["transitions"] = [transition()]
    atomic_json(path, spec)
    return path, spec


@pytest.mark.parametrize("version", [1, 2, 3])
def test_migration_preserves_file_and_legacy_fades(edit_assets, tmp_path, version):
    path, original = edit_assets
    spec = original if version == 1 else render.upgrade_plan(original)
    if version == 2:
        spec.update(schema_version=2)
        spec.pop("transitions")
    atomic_json(path, spec)
    before = path.read_bytes()
    store = EditorStore(path, tmp_path / "state")
    value = store.get(store.default_id)
    assert path.read_bytes() == before and value["plan"]["schema_version"] == 3
    assert value["plan"]["shots"][0]["fade_in_s"] == .1
    store.save(store.default_id, value["plan"], value["revision"])
    assert load_json(path)["schema_version"] == 3
    if version < 3:
        assert load_json(store.root / "backups" / f"{store.default_id}-v{version}.json")["schema_version"] == version


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_frame_timeline_no_drift_and_titles_absolute(edit_assets, fps):
    path, spec = modern(edit_assets)
    spec["settings"]["fps"] = fps
    spec["shots"] = [{"id": f"s{i}", "source_id": "фото.jpg", "duration_s": 1} for i in range(100)]
    spec["transitions"] = [transition(f"s{i}", f"s{i+1}", duration=1/fps) for i in range(99)]
    spec["titles"] = [{"id": "absolute", "text": "Абсолютное время", "start_s": 3, "end_s": 5}]
    resolved = render.resolve_plan(spec, path)
    assert resolved["frames"] == 100*fps-99
    assert resolved["shots"][-1]["timeline_start_frame"] == 99*(fps-1)
    assert resolved["titles"][0]["start_s"] == 3
    assert sum(p["frames"] for p in sections(resolved)) == resolved["frames"]


@pytest.mark.parametrize("change", ["nonadjacent", "duplicate", "unknown", "fractional", "zero", "negative", "old"])
def test_bad_transitions_rejected(edit_assets, change):
    path, spec = modern(edit_assets)
    if change == "nonadjacent": spec["transitions"][0]["to_shot_id"] = "s03"
    if change == "duplicate": spec["transitions"] *= 2
    if change == "unknown": spec["transitions"][0]["type"] = "zoom"
    if change == "fractional": spec["transitions"][0]["duration_s"] = .21
    if change == "zero": spec["transitions"][0]["duration_s"] = 0
    if change == "negative": spec["transitions"][0]["duration_s"] = -1
    if change == "old": spec["schema_version"] = 2
    with pytest.raises(AnalysisError): render.resolve_plan(spec, path)


def test_too_long_draft_persists_and_blocks_snapshot(edit_assets, tmp_path):
    path, spec = modern(edit_assets)
    spec["transitions"] = [transition(duration=.7), transition("s02", "s03", duration=.4)]
    atomic_json(path, spec)
    store = EditorStore(path, tmp_path / "state")
    value = store.get(store.default_id)
    assert any("самостоятельный кадр" in e for e in value["errors"])
    store.save(store.default_id, value["plan"], value["revision"])
    assert EditorStore(path, tmp_path / "state").get(store.default_id)["errors"]
    with pytest.raises(AnalysisError): store.snapshot(store.default_id, tmp_path/"snapshot.json", value["revision"])
    with pytest.raises(AnalysisError): render.load_plan(path)


@pytest.mark.parametrize("kind", ["crossfade", "fade_black"])
def test_all_media_pairs_render_and_preview(edit_assets, tmp_path, kind):
    path, spec = modern(edit_assets)
    sources = ["проезд.mp4", "дрон.mp4", "фото.jpg", "проезд.mp4", "фото.jpg", "фото.jpg"]
    spec["shots"] = [{"id": f"s{i}", "source_id": source,
                      **({"duration_s": 1} if source.endswith("jpg") else {"start_s": 0, "end_s": 1})}
                     for i, source in enumerate(sources)]
    spec["transitions"] = [transition(f"s{i}", f"s{i+1}", kind) for i in range(5)]
    spec["shots"][2]["audio"] = {"mode": "source", "source_id": "проезд.mp4", "start_s": 0, "gain": .5}
    spec["titles"] = [{"id": "title", "text": "Титр через стык", "start_s": .7, "end_s": 1.3}]
    atomic_json(path, spec)
    result = render.render(path, tmp_path / kind, cache=tmp_path / "cache")
    assert result["validation"]["frames"] == 150 and result["transition_count"] == 5
    assert result["validation"]["full_decode"] == "passed"
    plan = render.load_plan(path)
    info = preview_spec(plan, "s0", "s1")
    preview = render_preview(plan, "s0", "s1", tmp_path/"preview.mp4", tmp_path/"cache", tmp_path/"progress.json")
    assert preview["frames"] == info["frames"] and preview["full_decode"] == "passed"


def test_local_cache_changes_and_resume(edit_assets, tmp_path, monkeypatch):
    path, spec = modern(edit_assets)
    cache = tmp_path / "cache"
    first = render.render(path, tmp_path/"first", cache=cache)
    assert not any(s["cache_hit"] for s in first["shots"])
    spec["transitions"][0]["type"] = "fade_black"
    atomic_json(path, spec)
    second = render.render(path, tmp_path/"second", cache=cache)
    assert [s["cache_hit"] for s in second["shots"]] == [True, False, True, True]
    spec["transitions"][0]["duration_s"] = .3
    atomic_json(path, spec)
    third = render.render(path, tmp_path/"third", cache=cache)
    assert [s["cache_hit"] for s in third["shots"]] == [False, False, False, True]
    import aivideo.transitions as module
    original = module.render_section
    calls = []
    def interrupt(*args):
        calls.append(1)
        if len(calls) == 3: raise KeyboardInterrupt()
        return original(*args)
    monkeypatch.setattr(module, "render_section", interrupt)
    output = tmp_path/"interrupted"
    output.mkdir()
    (output/"film.mp4").write_bytes(b"previous")
    resume_cache = tmp_path/"resume-cache"
    with pytest.raises(KeyboardInterrupt): render.render(path, output, cache=resume_cache)
    assert (output/"film.mp4").read_bytes() == b"previous"
    monkeypatch.setattr(module, "render_section", original)
    assert [s["cache_hit"] for s in render.render(path, output, cache=resume_cache)["shots"]] == [True, True, False, False]


def test_title_is_drawn_after_black_transition_and_audio_has_no_gap(edit_assets, tmp_path):
    path, spec = modern(edit_assets)
    spec["shots"] = [{"id": f"s0{i}", "source_id": "фото.jpg", "duration_s": 1,
                      "audio": {"mode": "source", "source_id": "проезд.mp4", "start_s": 0}}
                     for i in [1, 2]]
    spec["transitions"] = [transition(kind="fade_black", duration=.4)]
    spec["titles"] = [{"id": "t", "text": "ТЕСТ", "start_s": .5, "end_s": 1.1}]
    plan = render.resolve_plan(spec, path)
    pieces = sections(plan)
    results = [render_section(p, plan["settings"], tmp_path/"cache", tool_version("ffmpeg")) for p in pieces]
    image = command([executable("ffmpeg"), "-v", "error", "-i", results[1]["path"],
                     "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], timeout=10).stdout
    frames = np.frombuffer(image, np.uint8).reshape(-1,180,320,3)
    darkest = np.argmin(frames[:,40,160,0])
    assert np.max(frames[darkest,20:60,100:200]) < 20
    assert np.sum(np.all(frames[darkest,100:175] > 230, axis=2)) > 20
    decoded = []
    for result in results:
        audio = command([executable("ffmpeg"), "-v", "error", "-i", result["path"], "-vn", "-f", "f32le", "-ac", "1", "-"], timeout=10).stdout
        samples = np.frombuffer(audio, np.float32)
        decoded.append(samples)
        assert np.sqrt(np.mean(samples[len(samples)//2-240:len(samples)//2+240]**2)) > .03
    audio = np.concatenate(decoded)
    for boundary in [len(decoded[0]),len(decoded[0])+len(decoded[1])]:
        assert np.sqrt(np.mean(audio[boundary-240:boundary+240]**2)) > .03


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_minimum_transition_exports_exact_frames(edit_assets, tmp_path, fps):
    path, spec = modern(edit_assets)
    spec["settings"]["fps"] = fps
    spec["shots"][2]["duration_s"] = 1
    spec["transitions"] = [transition(duration=1/fps), transition("s02", "s03", "fade_black", 1/fps)]
    atomic_json(path, spec)
    result = render.render(path, tmp_path/"film", cache=tmp_path/"cache")
    assert result["validation"]["frames"] == 3*fps-2
    assert result["validation"]["audio_duration_s"] == pytest.approx((3*fps-2)/fps, abs=.025)


def test_api_preview_revision_cache_and_manual_resume(local_editor):
    app, address = local_editor
    headers = {"X-Editor-Token": app.token}
    value = app.store.get(app.store.default_id)
    body = {"variant": app.store.default_id, "revision": "bad", "from_shot_id": "s01", "to_shot_id": "s02"}
    assert local_http.post(address+"/api/transition-preview", json=body, headers=headers).status_code == 409
    body["revision"] = value["revision"]
    response = local_http.post(address+"/api/transition-preview", json=body, headers=headers)
    assert response.status_code == 200, response.text
    job = response.json()
    snapshot = Path(job["snapshot"]).read_bytes()
    value["plan"]["shots"][0]["purpose"] = "Правка во время просмотра"
    app.store.save(app.store.default_id, value["plan"], value["revision"])
    assert Path(job["snapshot"]).read_bytes() == snapshot
    wait_for(lambda: load_json(app.jobs.path(job["id"]))["status"] in ("complete", "failed"))
    finished = next(j for j in local_http.get(address+"/api/jobs").json() if j["id"]==job["id"])
    assert finished["status"] == "complete", finished
    assert local_http.get(address+finished["url"], headers={"Range":"bytes=0-10"}).status_code == 206
    body["revision"] = app.store.get(app.store.default_id)["revision"]
    assert local_http.post(address+"/api/transition-preview", json=body, headers=headers).json()["cache_hit"]


def test_preview_queue_restart_requires_resume(edit_assets, tmp_path):
    path, _ = modern(edit_assets)
    store = EditorStore(path, tmp_path/"state")
    value = store.get(store.default_id)
    queue = JobQueue(store, cache=tmp_path/"cache")
    job = queue.transition_preview(store.default_id, value["revision"], "s01", "s02")
    queue.pause(job["id"])
    assert load_json(queue.path(job["id"]))["status"] == "paused"
    queue.resume(job["id"])
    queue = JobQueue(store, cache=tmp_path/"cache")
    queue.start()
    try:
        assert queue.list()[0]["status"] == "interrupted"
        queue.resume(job["id"])
        wait_for(lambda: load_json(queue.path(job["id"]))["status"] in ("complete", "failed"))
        assert load_json(queue.path(job["id"]))["status"] == "complete"
    finally: queue.close()
