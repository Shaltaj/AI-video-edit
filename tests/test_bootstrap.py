"""Exercise the actual Windows launcher under legacy console code pages."""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest

from aivideo.common import ROOT


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
@pytest.mark.parametrize("codepage", [866, 1251, 65001])
def test_ui_launcher_preserves_unicode_and_uses_portable_python(codepage, tmp_path):
    launcher = str(ROOT / "scripts/run.ps1").replace("'", "''")
    code = (
        f"[Console]::OutputEncoding = [Text.Encoding]::GetEncoding({codepage}); "
        "$ProgressPreference = 'SilentlyContinue'; "
        f"& '{launcher}' ui --help; exit $LASTEXITCODE"
    )
    env = os.environ.copy()
    env.update(UV_OFFLINE="1", VIRTUAL_ENV=str(ROOT / ".venv"))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
         base64.b64encode(code.encode("utf-16-le")).decode("ascii")],
        cwd=tmp_path, env=env, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"usage: aivideo ui" in result.stdout
    python = Path((ROOT / ".tools/bootstrap-python.txt").read_text(encoding="utf-8-sig").strip())
    assert python.is_file() and python.is_relative_to(ROOT / ".tools/python")
    result = subprocess.run(
        [str(python), "-c", "import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))"],
        capture_output=True, timeout=10, check=True,
    )
    prefix, base_prefix = json.loads(result.stdout)
    assert Path(prefix) == Path(base_prefix)
