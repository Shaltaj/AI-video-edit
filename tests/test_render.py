import pytest

from aivideo import render
from aivideo.common import AnalysisError, atomic_json, load_json


@pytest.mark.parametrize("change", ["bounds", "nan", "duration", "duplicate", "audio", "fractional", "evidence", "changed"])
def test_invalid_edits_fail_before_rendering(edit_assets, change):
    path, plan = edit_assets
    if change == "bounds":
        plan["shots"][0]["end_s"] = 5
    elif change == "nan":
        # Nonfinite JSON must be rejected even if supplied by an external editor.
        path.write_text(path.read_text(encoding="utf-8").replace('"end_s": 1.5', '"end_s": NaN'), encoding="utf-8")
    elif change == "duration":
        plan["shots"][0]["end_s"] = 0.5
    elif change == "duplicate":
        plan["shots"][1]["id"] = "s01"
    elif change == "audio":
        plan["shots"][1]["audio"]["start_s"] = 2.5
    elif change == "fractional":
        plan["shots"][0]["end_s"] = 1.51
    elif change == "evidence":
        (path.parent / "Анализ" / "segments.jsonl").write_text("", encoding="utf-8")
    elif change == "changed":
        (path.parent / "Исходники с пробелами" / "проезд.mp4").write_bytes(b"changed")
    if change != "nan":
        atomic_json(path, plan)
    with pytest.raises(AnalysisError):
        render.load_plan(path)


def test_render_real_media_and_reuse_after_text_edit(edit_assets, tmp_path):
    path, plan = edit_assets
    output, cache = tmp_path / "Ролик", tmp_path / "Кеш"
    captions = (tmp_path / "Анализ" / "segments.jsonl").read_bytes()
    result = render.render(path, output, cache=cache)
    assert result["status"] == "complete"
    assert render.load_plan(output / "plan.json")["frames"] == 75
    assert result["validation"]["frames"] == 75
    assert result["validation"]["audio_duration_s"] == pytest.approx(2.5, abs=0.025)
    assert result["validation"]["full_decode"] == "passed"
    assert not any(s["cache_hit"] for s in result["shots"])
    page = (output / "watch.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    previous = (output / "film.mp4").read_bytes()
    repeated = render.render(path, output, cache=cache)
    assert all(s["cache_hit"] for s in repeated["shots"])
    # A textual trim changes exactly one render operation; the source analysis is untouched.
    plan["shots"][0].update(start_s=0.6, end_s=1.6)
    atomic_json(path, plan)
    edited = render.render(path, output, cache=cache)
    assert [s["cache_hit"] for s in edited["shots"]] == [False, True, True]
    assert (output / "film.mp4").read_bytes() != previous
    assert (tmp_path / "Анализ" / "segments.jsonl").read_bytes() == captions
    plan["shots"][0], plan["shots"][1] = plan["shots"][1], plan["shots"][0]
    atomic_json(path, plan)
    reordered = render.render(path, tmp_path / "Перестановка", cache=cache)
    assert all(s["cache_hit"] for s in reordered["shots"])


def test_interruption_keeps_finished_shots_and_previous_movie(edit_assets, tmp_path, monkeypatch):
    path, plan = edit_assets
    output, cache = tmp_path / "Ролик", tmp_path / "Кеш"
    output.mkdir()
    (output / "film.mp4").write_bytes(b"previous movie")
    original = render.render_shot
    calls = 0
    def interrupt(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        return original(*args)
    monkeypatch.setattr(render, "render_shot", interrupt)
    with pytest.raises(KeyboardInterrupt):
        render.render(path, output, cache=cache)
    assert load_json(output / "render.json")["status"] == "interrupted"
    assert (output / "film.mp4").read_bytes() == b"previous movie"
    monkeypatch.setattr(render, "render_shot", original)
    result = render.render(path, output, cache=cache)
    assert [s["cache_hit"] for s in result["shots"]] == [True, False, False]


def test_source_and_analysis_cannot_be_render_destinations(edit_assets, tmp_path):
    path, _ = edit_assets
    with pytest.raises(AnalysisError):
        render.render(path, tmp_path / "Исходники с пробелами" / "Ролик", cache=tmp_path / "Кеш")
    with pytest.raises(AnalysisError):
        render.render(path, tmp_path / "Анализ", cache=tmp_path / "Кеш")
