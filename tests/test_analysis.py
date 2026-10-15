from pathlib import Path

from PIL import Image
import pytest

from aivideo import common, media, pipeline
from aivideo.common import (AnalysisError, atomic_json, cached_result, complete_result,
                            load_json, task_key)
from aivideo.report import media_url


def photo(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (96, 64), "green").save(path)


def test_intervals_cover_tail_without_overlap():
    intervals = media.windows(20.125)
    assert intervals == [(0.0, 10.0), (10.0, 20.0), (20.0, 20.125)]
    assert sum(end - start for start, end in intervals) == 20.125


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_rejected(duration):
    with pytest.raises(AnalysisError):
        media.windows(duration)


def test_unicode_paths_and_duplicate_names_are_distinct(tmp_path):
    source = tmp_path / "Поездка с пробелами"
    photo(source / "Камера один" / "снимок.jpg")
    photo(source / "Камера два" / "снимок.jpg")
    result = media.scan(source)
    assert not result["errors"]
    assert len({item["id"] for item in result["media"]}) == 2
    assert all(item["width"] == 96 and not item["has_audio"] for item in result["media"])


def test_preview_can_reference_media_on_another_windows_drive(tmp_path):
    path = Path('D:/Source/Поездка с пробелами/video.mp4')
    assert media_url(path, tmp_path) == path.as_uri()


def test_bad_video_is_reported_instead_of_disappearing(tmp_path):
    (tmp_path / "broken.mp4").write_bytes(b"broken")
    result = media.scan(tmp_path)
    assert not result["media"]
    assert result["errors"][0]["source_id"] == "broken.mp4"


def test_atomic_failure_preserves_previous_result(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_json(target, {"old": True})
    def fail(*_):
        raise OSError("simulated interruption")
    monkeypatch.setattr(common.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(target, {"new": True})
    assert load_json(target) == {"old": True}


def test_cache_invalidates_on_source_configuration_and_model_change(tmp_path):
    source = {"id": "GoPro/a.mp4", "source_root": str(tmp_path), "size_bytes": 12, "mtime_ns": 34}
    settings = {"start_s": 0, "end_s": 10, "fps": 2, "prompt": "v1"}
    model = {"revision": "first"}
    key = task_key("visual", source, settings, model)
    complete_result(tmp_path, key, {"description": "cached"})
    assert cached_result(tmp_path, key)["description"] == "cached"
    changes = [task_key("visual", {**source, "mtime_ns": 35}, settings, model),
               task_key("visual", source, {**settings, "fps": 1}, model),
               task_key("visual", source, {**settings, "prompt": "v2"}, model),
               task_key("visual", source, settings, {"revision": "second"})]
    assert all(cached_result(tmp_path, changed) is None for changed in changes)
    (tmp_path / f"{key}.json").write_text("{", encoding="utf-8")
    assert cached_result(tmp_path, key) is None
    atomic_json(tmp_path / f"{key}.json", [])
    assert cached_result(tmp_path, key) is None


@pytest.fixture
def video_collection(tmp_path):
    source = tmp_path / "Видео с пробелами"
    source.mkdir()
    for name, audio in (("со звуком.mp4", True), ("без звука.mp4", False)):
        arguments = [common.executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=20:duration=2.125"]
        if audio:
            arguments += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2.125"]
        arguments += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio:
            arguments += ["-c:a", "aac", "-shortest"]
        common.command(arguments + [source / name], timeout=30)
    return source


def test_actual_video_metadata_audio_and_timestamped_frames(video_collection, tmp_path, monkeypatch):
    index = media.scan(video_collection)
    assert not index["errors"]
    assert sorted(item["has_audio"] for item in index["media"]) == [False, True]
    silent = next(item for item in index["media"] if not item["has_audio"])
    prepared = media.prepare_frames(silent, 1.0, 2.1, tmp_path / "cache", 2, 96, "cpu")
    positions = [frame["source_time_s"] for frame in prepared["frames"]]
    assert positions and all(1.0 <= point < 2.1 for point in positions)
    assert positions[0] == 1.0
    for frame in prepared["frames"]:
        with Image.open(Path(prepared["folder"]) / frame["file"]) as image:
            assert max(image.size) <= 96
    short = media.prepare_frames(silent, 2.0, 2.1, tmp_path / "cache", 2, 96, "cpu")
    assert short["frames"][0]["source_time_s"] == 2.0
    voiced = next(item for item in index["media"] if item["has_audio"])
    audio = media.prepare_audio(voiced, 1.0, tmp_path / "cache")
    stream = media.probe(Path(audio["path"]))["streams"][0]
    assert stream["sample_rate"] == "16000" and stream["channels"] == 1
    from faster_whisper.audio import decode_audio
    assert len(decode_audio(audio["path"], sampling_rate=16000)) == 16000
    def must_not_execute(*_, **__):
        raise AssertionError("Completed preparation should be reused")
    monkeypatch.setattr(media, "command", must_not_execute)
    assert media.prepare_frames(silent, 1.0, 2.1, tmp_path / "cache", 2, 96, "cpu")["cache_hit"]


def test_interrupted_pipeline_resumes_only_missing_operations(tmp_path, monkeypatch):
    source = tmp_path / "Фото"
    photo(source / "1.jpg")
    photo(source / "2.jpg")
    monkeypatch.setattr(pipeline, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "doctor", lambda *_: {
        "models": {"visual": {"revision": "test", "path": "unused"}, "asr": {"revision": "test"}},
        "tools": {"ffmpeg": "test", "ffprobe": "test"}})
    seen = []
    interrupted = False
    def fake_worker(job, _folder):
        nonlocal interrupted
        for item in job["items"]:
            seen.append(item["source_id"])
            complete_result(Path(job["cache"]), item["key"], {
                "kind": "image", "source_id": item["source_id"], "description": "<script>bad</script>",
                "uncertainties": ["Проверить"], "metrics": {"wall_time_s": 0},
                "raw_response": "test", "start_s": None, "end_s": None})
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        atomic_json(Path(job["phase_output"]), {"kind": "visual", "metrics": {"wall_time_s": 0}})
    monkeypatch.setattr(pipeline, "run_worker", fake_worker)
    output = tmp_path / "output"
    with pytest.raises(KeyboardInterrupt):
        pipeline.analyze(source, output, "sample")
    assert load_json(output / "run.json")["status"] == "interrupted"
    assert len((output / "segments.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    result = pipeline.analyze(source, output, "sample")
    assert result["status"] == "complete"
    assert seen == ["1.jpg", "2.jpg"]
    assert result["cache_stats"]["visual"] == {"total": 2, "computed": 1, "cached": 1}
    page = (output / "preview.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;bad&lt;/script&gt;" in page
    assert "<script>bad</script>" not in page
    pipeline.analyze(source, output, "sample", fps=1)
    assert seen == ["1.jpg", "2.jpg", "1.jpg", "2.jpg"]
