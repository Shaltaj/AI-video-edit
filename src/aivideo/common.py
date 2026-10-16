from __future__ import annotations

import hashlib
import errno
from functools import lru_cache
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / ".tools"
RUNS = ROOT / "runs"
SCHEMA_VERSION = 1
_FILE_LOCK_ERRORS = frozenset({5, 32, 33})
_FILE_RETRY_TIMEOUT_S = 2.0
_PROGRESS_WARNINGS: dict[str, float] = {}
_LOGGER = logging.getLogger(__name__)


class AnalysisError(RuntimeError):
    pass


def _is_file_lock(exc: OSError) -> bool:
    # The Windows CRT can report sharing conflicts as EACCES without winerror
    # when opening a file; os.replace usually reports the native Windows code.
    return (getattr(exc, "winerror", None) in _FILE_LOCK_ERRORS
            or (os.name == "nt" and isinstance(exc, PermissionError)
                and exc.errno in (errno.EACCES, errno.EPERM)))


def check_pause() -> None:
    marker = os.environ.get("AIVIDEO_PAUSE_FILE")
    if marker and Path(marker).exists():
        raise KeyboardInterrupt("Обработка приостановлена")


def progress(stage: str, done=0, total=0, cached=0) -> None:
    target = os.environ.get("AIVIDEO_PROGRESS_FILE")
    if target:
        try:
            atomic_json(Path(target), {"stage": stage, "done": done, "total": total, "cached": cached})
        except OSError as exc:
            if not _is_file_lock(exc):
                raise
            # Telemetry must not invalidate a completed preparation or model result.
            # Durable run/job/cache writes still propagate failures after retries.
            current = time.monotonic()
            if current - _PROGRESS_WARNINGS.get(target, float("-inf")) >= 60:
                _LOGGER.warning("Не удалось обновить индикатор прогресса %s: %s. Обработка продолжается; следующее обновление повторит запись.", target, exc)
                _PROGRESS_WARNINGS[target] = current


def _retry_file_lock(operation):
    """Retry short Windows sharing conflicts without changing the operation."""
    deadline = time.monotonic() + _FILE_RETRY_TIMEOUT_S
    delay = .01
    while True:
        try:
            return operation()
        except OSError as exc:
            remaining = deadline - time.monotonic()
            if not _is_file_lock(exc) or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, .25)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _retry_file_lock(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path):
    # Close the read handle before parsing so Windows writers only wait on I/O.
    content = _retry_file_lock(lambda: path.read_text(encoding="utf-8"))
    return json.loads(content)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _retry_file_lock(lambda: os.replace(temporary, path))
    finally:
        temporary.unlink(missing_ok=True)


def digest(value: object) -> str:
    content = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def versions() -> dict:
    names = ("aivideo", "torch", "torchvision", "transformers", "bitsandbytes",
             "accelerate", "faster-whisper", "ctranslate2", "av", "onnxruntime", "pillow", "numpy")
    result = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not installed"
    return result


def executable(name: str) -> Path:
    path = TOOLS / "ffmpeg" / "bin" / f"{name}.exe"
    if not path.is_file():
        raise AnalysisError(f"Не найден {path}. Запустите scripts/setup.ps1.")
    return path


def command(arguments: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        check_pause()
        began = time.monotonic()
        child = subprocess.Popen([str(arg) for arg in arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                check_pause()
                if timeout is not None and time.monotonic() - began > timeout:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                try:
                    stdout, stderr = child.communicate(timeout=0.2)
                    process = subprocess.CompletedProcess(arguments, child.returncode, stdout, stderr)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            child.kill()
            child.communicate()
            raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnalysisError(str(exc)) from exc
    if process.returncode:
        details = process.stderr.decode("utf-8", errors="replace")[-12000:]
        raise AnalysisError(f"Команда завершилась с кодом {process.returncode}:\n{arguments}\n{details}")
    return process


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{milliseconds:03}"


@lru_cache(maxsize=4)
def tool_version(name: str) -> str:
    return command([executable(name), "-version"], timeout=30).stdout.decode("utf-8", "replace").splitlines()[0]


def cached_result(cache: Path, key: str) -> dict | None:
    try:
        value = load_json(cache / f"{key}.json")
        if isinstance(value, dict) and value.get("status") == "complete" and value.get("key") == key:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def complete_result(cache: Path, key: str, value: dict) -> dict:
    value = {**value, "schema_version": SCHEMA_VERSION, "status": "complete", "key": key}
    atomic_json(cache / f"{key}.json", value)
    return value


def task_key(kind: str, media: dict, settings: dict, model: dict | None = None) -> str:
    return digest({"schema": SCHEMA_VERSION, "kind": kind,
                   "source": media["source_root"], "id": media["id"],
                   "size": media["size_bytes"], "mtime_ns": media["mtime_ns"],
                   "settings": settings, "model": model})


def offline_environment() -> dict:
    environment = os.environ.copy()
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                        "HF_HOME": str(TOOLS / "huggingface"), "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                        "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false"})
    return environment


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
