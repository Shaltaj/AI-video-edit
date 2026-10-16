"""Atomic state writes remain usable during Windows UI/worker contention."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from aivideo import common, pipeline


def file_lock(code=5):
    error = PermissionError(errno.EACCES, "Simulated Windows file lock")
    error.winerror = code
    return error


@pytest.mark.parametrize("kind", ["json", "text"])
@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_temporary_file_lock_preserves_old_data_then_replaces(tmp_path, monkeypatch, kind, winerror):
    path = tmp_path/"Кириллица с пробелами"/"state.json"
    common.atomic_json(path, {"value":"old"})
    replace = common.os.replace
    attempts = 0
    def locked_then_available(temporary, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            assert path.read_text(encoding="utf-8").strip() == '{\n  "value": "old"\n}'
            raise file_lock(winerror)
        return replace(temporary, target)
    monkeypatch.setattr(common.os,"replace",locked_then_available)
    monkeypatch.setattr(common.time,"sleep",lambda _:None)
    if kind=="json":
        common.atomic_json(path, {"value":"new"})
    else:
        common.atomic_text(path, '{"value":"new"}')
    assert attempts==3 and common.load_json(path)=={"value":"new"}
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["json", "text"])
def test_persistent_lock_is_not_hidden_for_durable_state(tmp_path, monkeypatch, kind):
    path = tmp_path/"run.json"
    common.atomic_json(path,{"old":True})
    monkeypatch.setattr(common,"_FILE_RETRY_TIMEOUT_S",0)
    def denied(*_):
        raise file_lock()
    monkeypatch.setattr(common.os,"replace",denied)
    with pytest.raises(PermissionError):
        if kind=="json":
            common.atomic_json(path,{"new":True})
        else:
            common.atomic_text(path,"new")
    assert common.load_json(path)=={"old":True}
    assert not list(tmp_path.glob("*.tmp"))


def test_blocked_progress_warns_once_and_recovers(tmp_path, monkeypatch, caplog):
    path=tmp_path/"progress.json"
    common.atomic_json(path,{"done":0})
    monkeypatch.setenv("AIVIDEO_PROGRESS_FILE",str(path))
    monkeypatch.setattr(common,"_FILE_RETRY_TIMEOUT_S",0)
    replace=common.os.replace
    def locked(*_):
        raise file_lock()
    monkeypatch.setattr(common.os,"replace",locked)
    common.progress("Подготовка",1,3)
    common.progress("Подготовка",2,3)
    assert common.load_json(path)=={"done":0}
    assert len(caplog.records)==1 and "Обработка продолжается" in caplog.text
    monkeypatch.setattr(common.os,"replace",replace)
    common.progress("Подготовка",3,3)
    assert common.load_json(path)["done"]==3
    assert not list(tmp_path.glob("*.tmp"))


def test_disk_failure_is_not_ignored_or_retried(tmp_path, monkeypatch):
    path=tmp_path/"progress.json"
    monkeypatch.setenv("AIVIDEO_PROGRESS_FILE",str(path))
    calls=[]
    def disk_error(*args):
        calls.append(args)
        raise OSError(errno.ENOSPC,"Disk full")
    monkeypatch.setattr(common.os,"replace",disk_error)
    with pytest.raises(OSError,match="Disk full"):
        common.progress("Подготовка",1,2)
    assert len(calls)==1


def test_json_reader_releases_file_before_parsing(tmp_path, monkeypatch):
    path=tmp_path/"Состояние с пробелами.json"
    common.atomic_json(path,{"version":1})
    decode = json.loads
    def parse_while_updating(content):
        common.atomic_json(path,{"version":2})
        return decode(content)
    monkeypatch.setattr(common.json,"loads",parse_while_updating)
    assert common.load_json(path)=={"version":1}
    assert decode(path.read_text(encoding="utf-8"))=={"version":2}


@pytest.mark.skipif(os.name!="nt",reason="Windows CRT sharing errors")
def test_json_read_retries_crt_access_denied_without_winerror(tmp_path, monkeypatch):
    path=tmp_path/"run.json"
    common.atomic_json(path,{"value":1})
    read=Path.read_text
    calls=[]
    def temporarily_busy(target,*args,**kwargs):
        calls.append(target)
        if len(calls)<=2:
            raise PermissionError(errno.EACCES,"Permission denied")
        return read(target,*args,**kwargs)
    monkeypatch.setattr(Path,"read_text",temporarily_busy)
    monkeypatch.setattr(common.time,"sleep",lambda _:None)
    assert common.load_json(path)=={"value":1} and len(calls)==3


@pytest.mark.skipif(os.name!="nt",reason="Windows sharing semantics")
def test_actual_windows_reader_lock_is_retried(tmp_path, monkeypatch):
    path=tmp_path/"progress.json"
    common.atomic_json(path,{"done":0})
    observed=threading.Event()
    failures=[]
    replace=common.os.replace
    def observe_lock(temporary,target):
        try:
            return replace(temporary,target)
        except PermissionError:
            observed.set()
            raise
    monkeypatch.setattr(common.os,"replace",observe_lock)
    def write():
        try:
            common.atomic_json(path,{"done":1})
        except Exception as exc:
            failures.append(exc)
    with path.open("rb"):
        writer=threading.Thread(target=write)
        writer.start()
        assert observed.wait(2),"Native Windows read handle did not block replacement"
        assert common.load_json(path)=={"done":0}
    writer.join(4)
    assert not writer.is_alive() and not failures
    assert common.load_json(path)=={"done":1}


def test_worker_updates_while_ui_polls_from_another_process(tmp_path):
    path=tmp_path/"Папка проекта"/"progress.json"
    common.atomic_json(path,{"done":0,"payload":"0"*256})
    stop=threading.Event()
    failures=[]
    reads=[]
    def poll():
        while not stop.is_set():
            try:
                value=common.load_json(path)
                assert value["payload"]==str(value["done"])*256
                reads.append(value["done"])
            except Exception as exc:
                failures.append(exc)
                return
            stop.wait(.001)
    reader=threading.Thread(target=poll)
    reader.start()
    code="""from pathlib import Path
import sys
from aivideo.common import atomic_json
for n in range(1,201):
    atomic_json(Path(sys.argv[1]),{'done':n,'payload':str(n)*256})
"""
    env=os.environ.copy()
    env["PYTHONPATH"]=str(common.ROOT/"src")
    try:
        result=subprocess.run([sys.executable,"-c",code,str(path)],env=env,capture_output=True,timeout=15)
    finally:
        stop.set()
        reader.join(3)
    assert result.returncode==0,result.stderr.decode("utf-8","replace")
    assert not failures and len(reads)>1
    assert common.load_json(path)["done"]==200


def test_progress_lock_does_not_abort_analysis_or_invalidate_cache(tmp_path, monkeypatch):
    from PIL import Image
    source=tmp_path/"Исходники"
    source.mkdir()
    Image.new("RGB",(64,64),"green").save(source/"Фото.jpg")
    target=tmp_path/"jobs/progress.json"
    common.atomic_json(target,{"done":0})
    monkeypatch.setenv("AIVIDEO_PROGRESS_FILE",str(target))
    monkeypatch.setattr(common,"_FILE_RETRY_TIMEOUT_S",0)
    replace=common.os.replace
    def lock_progress(temporary,path):
        if path==target:
            raise file_lock()
        return replace(temporary,path)
    monkeypatch.setattr(common.os,"replace",lock_progress)
    monkeypatch.setattr(pipeline,"doctor",lambda *_:{
        "models":{"visual":{"revision":"test"},"asr":{"revision":"test"}},
        "tools":{"ffmpeg":"test","ffprobe":"test"}})
    seen=[]
    def worker(job,_folder):
        for item in job["items"]:
            seen.append(item["source_id"])
            common.complete_result(Path(job["cache"]),item["key"],{
                "source_id":item["source_id"],"kind":"image","start_s":None,"end_s":None,
                "description":"Фото","uncertainties":[],"raw_response":"Фото","metrics":{}})
        common.atomic_json(Path(job["phase_output"]),{"kind":"visual","metrics":{}})
    monkeypatch.setattr(pipeline,"run_worker",worker)
    first=pipeline.analyze(source,tmp_path/"analysis","sample",cache_root=tmp_path/"cache")
    second=pipeline.analyze(source,tmp_path/"analysis","sample",cache_root=tmp_path/"cache")
    assert first["status"]==second["status"]=="complete"
    assert seen==["Фото.jpg"] and second["cache_stats"]["visual"]["cached"]==1
    assert common.load_json(target)=={"done":0}
