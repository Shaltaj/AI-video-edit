import json
from PIL import Image
import pytest

from aivideo import common, media
from aivideo.common import atomic_json


@pytest.fixture
def edit_assets(tmp_path):
    source = tmp_path / "Исходники с пробелами"
    source.mkdir()
    for name, sound in (("проезд.mp4", True), ("дрон.mp4", False)):
        args = [common.executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=30:duration=3"]
        if sound:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3"]
        args += ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p"]
        if sound:
            args += ["-c:a", "aac", "-shortest"]
        common.command(args + [source / name], timeout=30)
    Image.new("RGB", (80, 100), "red").save(source / "фото.jpg")
    analysis = tmp_path / "Анализ"
    index = media.scan(source)
    atomic_json(analysis / "media-index.json", index)
    evidence = [{"key": item["id"], "source_id": item["id"], "kind": item["kind"],
                 "start_s": 0 if item["kind"] == "video" else None,
                 "end_s": 3 if item["kind"] == "video" else None,
                 "description": "Тестовый проезд " + item["id"], "uncertainties": []} for item in index["media"]]
    common.atomic_text(analysis / "segments.jsonl", "".join(json.dumps(e) + "\n" for e in evidence))
    plan = {"schema_version": 1, "analysis": "Анализ", "title": "Тест <script>alert(1)</script>",
            "settings": {"width": 320, "height": 180, "fps": 30, "encoder": "libx264", "decode": "cpu"},
            "shots": [
                {"id": "s01", "source_id": "проезд.mp4", "start_s": 0.5, "end_s": 1.5,
                 "title": "Проверка: 100% 'привет'", "fade_in_s": 0.1},
                {"id": "s02", "source_id": "дрон.mp4", "start_s": 0.2, "end_s": 1.2,
                 "audio": {"mode": "source", "source_id": "проезд.mp4", "start_s": 1.5, "gain": 0.5}},
                {"id": "s03", "source_id": "фото.jpg", "duration_s": 0.5,
                 "fade_out_s": 0.1, "audio": {"mode": "silence"}}]}
    path = tmp_path / "plan.json"
    atomic_json(path, plan)
    return path, plan
