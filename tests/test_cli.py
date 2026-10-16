import sys

from PIL import Image
import pytest

from aivideo import cli, media, pipeline
from aivideo.common import load_json


@pytest.mark.parametrize("command", ["scan", "analyze"])
def test_source_is_required_before_processing(command, tmp_path, monkeypatch, capsys):
    def must_not_process(*_, **__):
        pytest.fail("Missing --source must be rejected before processing")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media, "scan", must_not_process)
    monkeypatch.setattr(pipeline, "analyze", must_not_process)
    monkeypatch.setattr(sys, "argv", ["aivideo", command, "--output", str(tmp_path / "output")])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--source" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_scan_uses_explicit_source(tmp_path, monkeypatch):
    source = tmp_path / "Исходники с пробелами"
    source.mkdir()
    Image.new("RGB", (32, 24), "blue").save(source / "Фото.jpg")
    output = tmp_path / "analysis" / "media-index.json"
    monkeypatch.setattr(sys, "argv", ["aivideo", "scan", "--source", str(source),
                                      "--output", str(output)])
    assert cli.main() == 0
    index = load_json(output)
    assert index["source_root"] == str(source.resolve())
    assert [item["id"] for item in index["media"]] == ["Фото.jpg"]
