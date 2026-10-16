"""Independent readiness checks, available from the dependency-free UI runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import tomllib

from .common import AnalysisError, ROOT, atomic_json, check_pause, command, now, progress
from .models import MODEL_IDS, missing_files

INSTALLABLE = {"runtime", "ffmpeg", "visual", "asr"}


def diagnose(output: Path, root: Path = ROOT):
    output.mkdir(parents=True, exist_ok=True)
    python = root / ".venv/Scripts/python.exe"
    rows = []
    def add(key, label, state, details, install=None):
        rows.append({"id": key, "label": label, "status": state, "details": details,
                     "install": install, "installable": install in INSTALLABLE})
    progress("Проверка среды обработки")
    runtime_ok = False
    if python.is_file():
        try:
            requirements = tomllib.loads((root/"pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
            code = ("import importlib.metadata as m,json,re; req=" + repr(requirements) + "; "
                    "result={};\nfor r in req:\n n=re.split(r'[<>=\\[]',r)[0]\n"
                    " try: result[n]=m.version(n)\n except m.PackageNotFoundError: result[n]=None\n"
                    "import sys;result['python']='.'.join(map(str,sys.version_info[:3]));print(json.dumps(result))")
            versions = json.loads(command([python,"-c",code], timeout=30).stdout)
            missing = [r for r in requirements if not versions.get(__import__('re').split(r'[<>=\[]',r)[0])]
            if not versions.get('python','').startswith('3.12.'):
                missing.append('Python 3.12')
            for requirement in requirements:
                if "==" in requirement:
                    name, expected = requirement.split("==",1)
                    if (versions.get(name) or "").split("+")[0] != expected:
                        missing.append(requirement)
            runtime_ok = not missing
            add("runtime", "Python и зависимости", "ready" if runtime_ok else "missing",
                json.dumps(versions,ensure_ascii=False) if runtime_ok else "Не установлены или не совпадают версии: " + ", ".join(missing), "runtime")
        except Exception as exc:
            add("runtime", "Python и зависимости", "error", str(exc), "runtime")
    else:
        add("runtime", "Python и зависимости", "missing", "Среда обработки не установлена", "runtime")
    for tool in ("ffmpeg", "ffprobe"):
        path = root / f".tools/ffmpeg/bin/{tool}.exe"
        try:
            version = command([path,"-version"], timeout=15).stdout.decode("utf-8","replace").splitlines()[0]
            add(tool, tool, "ready", version, "ffmpeg")
        except Exception as exc:
            add(tool, tool, "missing" if not path.is_file() else "error", str(exc), "ffmpeg")
    nvidia_ok = False
    try:
        info = command(["nvidia-smi","--query-gpu=name,driver_version,memory.total","--format=csv,noheader"],timeout=15).stdout.decode("utf-8","replace").strip()
        nvidia_ok = bool(info)
        add("nvidia", "Видеокарта и драйвер NVIDIA", "ready" if nvidia_ok else "error", info)
    except Exception as exc:
        add("nvidia", "Видеокарта и драйвер NVIDIA", "error", str(exc))
    library = root / ".venv/Lib/site-packages/torch/lib"
    missing = [n for n in ("cublas64_12.dll", "cudnn64_9.dll") if not (library/n).is_file()]
    add("cuda", "Локальные CUDA/cuDNN библиотеки", "missing" if missing else "ready", ", ".join(missing) if missing else str(library), "runtime")
    if runtime_ok and nvidia_ok and not missing:
        try:
            progress("Проверка операции на GPU")
            job = output/"gpu-job.json"
            atomic_json(job, {"mode":"doctor","output":str(output/"gpu.json"),"error_output":str(output/"gpu-error.json")})
            result = command([python,"-m","aivideo.worker",job],timeout=120)
            (output/"gpu.log").write_bytes(result.stdout + result.stderr)
            add("gpu", "Операция на GPU, NF4 и распознавание речи", "ready", json.dumps(json.loads((output/"gpu.json").read_text(encoding="utf-8")),ensure_ascii=False))
        except Exception as exc:
            add("gpu", "Операция на GPU, NF4 и распознавание речи", "error", str(exc))
    else:
        add("gpu", "Операция на GPU, NF4 и распознавание речи", "blocked", "Сначала исправьте среду обработки, библиотеки CUDA или драйвер")
    try:
        manifest = json.loads((root/".tools/models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    for kind, repo in MODEL_IDS.items():
        missing = missing_files(manifest.get(kind,{}), kind, root)
        add(kind, repo, "missing" if missing else "ready", ", ".join(missing) if missing else "Все необходимые файлы на месте", kind)
    result = {"created_at":now(),"checks":rows,"ready":all(r["status"]=="ready" for r in rows),
              "driver_url":"https://www.nvidia.com/Download/index.aspx"}
    atomic_json(output/"diagnostics.json",result)
    return result


def run_install(arguments, cwd=ROOT):
    """Stop the owned installer process tree on pause; it can be retried safely."""
    process = subprocess.Popen([str(a) for a in arguments], cwd=cwd)
    try:
        while process.poll() is None:
            check_pause()
            time.sleep(.2)
        if process.returncode:
            raise AnalysisError(f"Установщик завершился с кодом {process.returncode}")
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill","/PID",str(process.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else:
                process.kill()
            process.wait()
        raise


def install(components, output, root=ROOT):
    if not isinstance(components,list) or not components or set(components)-INSTALLABLE:
        raise AnalysisError("Укажите компоненты приложения для установки")
    before = diagnose(output, root)
    ready = {r["id"]:r["status"]=="ready" for r in before["checks"]}
    for number, component in enumerate(dict.fromkeys(components),1):
        check_pause()
        if component == "ffmpeg" and ready.get("ffmpeg") and ready.get("ffprobe"):
            continue
        if component != "ffmpeg" and ready.get(component) and (component != "runtime" or ready.get("cuda")):
            continue
        progress("Установка: "+component,number-1,len(components))
        if component in ("visual","asr"):
            if not ready.get("runtime"):
                run_install(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",root/"scripts/setup.ps1","-Component","runtime"],root)
                ready["runtime"] = True
            run_install(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",root/"scripts/download-models.ps1","--kind",component],root)
        else:
            args = ["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",root/"scripts/setup.ps1","-Component",component]
            if component == "runtime" and ready.get("runtime") and not ready.get("cuda"):
                args += ["-RepairTorch"]
            if component == "ffmpeg":
                args += ["-RepairFFmpeg"]
            run_install(args,root)
            ready[component] = True
            if component == "runtime":
                ready["cuda"] = True
    return diagnose(output,root)
