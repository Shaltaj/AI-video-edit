import json
import os
from pathlib import Path

from PIL import Image
import pytest

from aivideo import media, pipeline, render
from aivideo.common import AnalysisError, atomic_json, atomic_text, complete_result, load_json
from aivideo.handoff import catalog, export_markdown
from aivideo.images import open_photo, prepare_photo, register_heif
from aivideo.projects import ProjectRegistry


def heif(path):
    register_heif()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", (120, 80), "green") as image:
        image.save(path, format="HEIF", quality=95)


@pytest.mark.parametrize("suffix", [".heic", ".HEIF"])
def test_heif_scan_lossless_input_and_cache(tmp_path, monkeypatch, suffix):
    path = tmp_path / "Фото с пробелами" / ("Снимок" + suffix)
    heif(path)
    original = path.read_bytes()
    index = media.scan(path.parent)
    assert not index["errors"] and len(index["media"]) == 1
    item = index["media"][0]
    assert (item["width"], item["height"]) == (120, 80)
    prepared = prepare_photo(item, tmp_path / "cache")
    with Image.open(prepared["path"]) as image:
        assert image.format == "PNG" and image.size == (120, 80)
    def no_decode(*_):
        raise AssertionError("Must reuse prepared photo")
    monkeypatch.setattr("aivideo.images.open_photo", no_decode)
    assert prepare_photo(item, tmp_path / "cache")["cache_hit"]
    assert path.read_bytes() == original


def test_unreadable_and_excluded_sources_do_not_block_analysis(tmp_path, monkeypatch):
    source = tmp_path / "Исходники"
    heif(source / "Фото.heic")
    (source / "Повреждённое фото.jpg").write_bytes(b"broken photo")
    excluded = source / "Не анализировать.mp4"
    excluded.write_bytes(b"broken video")
    registry = ProjectRegistry(tmp_path/"state", tmp_path/"application")
    project = registry.create("Проверка пропуска", source, tmp_path/"Рабочая папка")
    value = project.read()
    value["excluded_sources"] = [excluded.name]
    atomic_json(project.path, value)
    def no_probe(*_):
        raise AssertionError("Excluded video must not be probed")
    monkeypatch.setattr(media, "probe", no_probe)
    monkeypatch.setattr(pipeline, "doctor", lambda *_: {
        "models": {"visual": {"revision":"test"}, "asr": {"revision":"test"}},
        "tools": {"ffmpeg":"test", "ffprobe":"test"}})
    seen = []
    def fake_worker(job, _folder):
        for item in job["items"]:
            with open_photo(item["frames"][0]["path"]) as image:
                assert image.size == (120, 80)
            seen.append(item["source_id"])
            complete_result(Path(job["cache"]), item["key"], {
                "kind":"image", "source_id":item["source_id"], "description":"Зелёная фотография",
                "uncertainties":[], "metrics":{"wall_time_s":0}, "raw_response":"Тест",
                "start_s":None, "end_s":None})
        atomic_json(Path(job["phase_output"]), {"kind":"visual", "metrics":{"wall_time_s":0}})
    monkeypatch.setattr(pipeline, "run_worker", fake_worker)
    kwargs = {"cache_root":project.folder("cache")/"analysis", "relative_paths":True,
              "excluded_sources":value["excluded_sources"]}
    result = pipeline.analyze(source, project.folder("analysis"), "sample", **kwargs)
    assert result["status"] == "complete" and seen == ["Фото.heic"]
    assert result["skipped_sources"][0]["source_id"] == "Повреждённое фото.jpg"
    assert excluded.is_file() and len(catalog(project)["visual"]) == 1
    assert 'image.png"' in (project.folder("analysis")/"preview.html").read_text(encoding="utf-8")
    _, text, _ = export_markdown(project)
    assert "Ошибка чтения" in text and "Исключён пользователем" in text
    assert excluded.name in text and "Повреждённое фото.jpg" in text
    assert str(source) not in text
    repeated = pipeline.analyze(source, project.folder("analysis"), "sample", **kwargs)
    assert repeated["cache_stats"]["visual"]["cached"] == 1 and len(seen) == 1
    (source / "Повреждённое фото.jpg").write_bytes(b"changed broken photo")
    with pytest.raises(AnalysisError, match="изменился"):
        catalog(project)


def test_heif_render_with_cuts_and_both_transitions(edit_assets, tmp_path):
    path, original = edit_assets
    source = path.parent / "Исходники с пробелами"
    heif(source / "Фото.heic")
    analysis = path.parent / "Анализ"
    atomic_json(analysis / "media-index.json", media.scan(source))
    entry = {"key":"heif", "source_id":"Фото.heic", "kind":"image", "start_s":None,
             "end_s":None, "description":"Фото", "uncertainties":[]}
    atomic_text(analysis/"segments.jsonl", (analysis/"segments.jsonl").read_text(encoding="utf-8") + json.dumps(entry) + "\n")
    plan = {"schema_version":3, "analysis":"Анализ", "settings":original["settings"], "title":"HEIC",
            "shots":[{"id":"a", "source_id":"Фото.heic", "duration_s":1},
                     {"id":"b", "source_id":"проезд.mp4", "start_s":0, "end_s":1},
                     {"id":"c", "source_id":"Фото.heic", "duration_s":1}], "titles":[], "transitions":[]}
    atomic_json(path, plan)
    plain = render.render(path, tmp_path/"Обычные склейки", cache=tmp_path/"cache")
    assert plain["validation"]["frames"] == 90
    plan["transitions"] = [
        {"from_shot_id":"a", "to_shot_id":"b", "type":"crossfade", "duration_s":.2},
        {"from_shot_id":"b", "to_shot_id":"c", "type":"fade_black", "duration_s":.2}]
    atomic_json(path, plan)
    mixed = render.render(path, tmp_path/"Плавные переходы", cache=tmp_path/"cache")
    assert mixed["validation"]["frames"] == 78 and mixed["validation"]["full_decode"] == "passed"
    from aivideo.editor import EditorApp
    item = next(m for m in load_json(analysis/"media-index.json")["media"] if m["id"]=="Фото.heic")
    prepared = prepare_photo(item,tmp_path/"cache")
    atomic_json(analysis/"run.json",{"relative_paths":True,"preparation":[{
        "kind":"photo","source_id":"Фото.heic","path":os.path.relpath(prepared["path"],analysis)}]})
    app = EditorApp(path,state_root=tmp_path/"editor-state",baselines_root=tmp_path/"renders")
    try:
        thumbnail = app.thumbnail_path(entry)
        assert thumbnail.suffix==".png" and thumbnail.is_file()
        assert next(e for e in app.catalog_payload()["entries"] if e["source_id"]=="Фото.heic")["thumbnail"].startswith("/media/")
    finally:
        app.close()


@pytest.mark.parametrize("profile", ["smoke", "sample"])
@pytest.mark.parametrize("availability", ["empty", "unreadable", "excluded"])
def test_no_readable_sources_fails_before_loading_gpu(tmp_path, monkeypatch, profile, availability):
    source = tmp_path/"Исходники"
    source.mkdir()
    if availability != "empty":
        (source/"Повреждённое.jpg").write_bytes(b"broken")
    excluded = ["Повреждённое.jpg"] if availability == "excluded" else []
    def no_gpu(*_):
        raise AssertionError("No GPU work for unreadable sources")
    monkeypatch.setattr(pipeline,"doctor",no_gpu)
    with pytest.raises(AnalysisError,match="Нет доступных материалов"):
        pipeline.analyze(source,tmp_path/"analysis",profile,cache_root=tmp_path/"cache",
                         excluded_sources=excluded)
    assert load_json(tmp_path/"analysis/run.json")["status"]=="error"
