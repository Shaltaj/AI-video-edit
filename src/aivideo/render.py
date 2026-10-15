"""Local rendering of an explicit edit decision list; no model calls or network."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .common import (AnalysisError, ROOT, RUNS, atomic_json, atomic_text, digest,
                     executable, load_json, now, timestamp, tool_version)
from .media import probe
from .report import media_url

RENDER_VERSION = 1


class RenderPaused(KeyboardInterrupt):
    """Cooperative cancellation retains completed fragments."""


def check_pause() -> None:
    marker = os.environ.get("AIVIDEO_PAUSE_FILE")
    if marker and Path(marker).exists():
        raise RenderPaused("Обработка приостановлена")


def upgrade_plan(spec: dict) -> dict:
    """Canonical editor representation. Never changes the supplied document."""
    value = json.loads(json.dumps(spec, ensure_ascii=False, allow_nan=False))
    if value.get("schema_version") == 1:
        titles, cursor = [], 0.0
        for shot in value.get("shots", []):
            duration = shot.get("duration_s", shot.get("end_s", 0) - shot.get("start_s", 0))
            if shot.get("title"):
                titles.append({"id": "title-" + shot["id"], "text": shot["title"],
                               "start_s": cursor, "end_s": round(cursor + duration, 6)})
            shot.pop("title", None)
            cursor = round(cursor + duration, 6)
        value.update(schema_version=2, titles=titles)
    return value


def number(value, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise AnalysisError(f"{label}: требуется число")
    if not math.isfinite(value) or value < minimum:
        raise AnalysisError(f"{label}: недопустимое значение {value}")
    return float(value)


def load_plan(path: Path) -> dict:
    path = path.resolve()
    return resolve_plan(load_json(path), path)


def resolve_plan(plan: dict, path: Path, *, allow_title_errors: bool = False) -> dict:
    path = path.resolve()
    if not isinstance(plan, dict) or plan.get("schema_version") not in (1, 2):
        raise AnalysisError("Неизвестная версия монтажного плана")
    analysis = (path.parent / plan["analysis"]).resolve()
    index = load_json(analysis / "media-index.json")
    source = Path(index["source_root"]).resolve()
    library = {item["id"]: item for item in index["media"]}
    visual = [json.loads(line) for line in (analysis / "segments.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    settings = {"width": 1920, "height": 1080, "fps": 30,
                "encoder": "h264_nvenc", "decode": "cuda", **plan.get("settings", {})}
    unknown = settings.keys() - {"width", "height", "fps", "encoder", "decode"}
    if unknown:
        raise AnalysisError(f"Неизвестные настройки: {sorted(unknown)}")
    for field in ("width", "height", "fps"):
        if type(settings[field]) is not int:
            raise AnalysisError(f"{field}: требуется целое число")
    if any(not 64 <= settings[k] <= 3840 or settings[k] % 2 for k in ("width", "height")):
        raise AnalysisError("Размер кадра: чётные числа 64..3840")
    if settings["fps"] not in (24, 25, 30, 50, 60):
        raise AnalysisError("fps: допустимы 24, 25, 30, 50, 60")
    if settings["encoder"] not in ("h264_nvenc", "libx264") or settings["decode"] not in ("cuda", "cpu"):
        raise AnalysisError("Неизвестный кодировщик или декодер")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 1000:
        raise AnalysisError("Нужен непустой список shots, максимум 1000 сцен")
    seen, verified, resolved, cursor_frames = set(), {}, [], 0

    def material(source_id):
        if source_id not in library:
            raise AnalysisError(f"Исходник отсутствует в каталоге: {source_id}")
        item = library[source_id]
        full = (source / item["relative_path"]).resolve()
        if source not in full.parents:
            raise AnalysisError(f"Путь выходит за папку исходников: {source_id}")
        if source_id not in verified:
            stat = full.stat()
            if (stat.st_size, stat.st_mtime_ns) != (item["size_bytes"], item["mtime_ns"]):
                raise AnalysisError(f"Исходник изменился, обновите анализ: {source_id}")
            verified[source_id] = {**item, "path": str(full)}
        return verified[source_id]

    for original in shots:
        if not isinstance(original, dict):
            raise AnalysisError("Каждая сцена должна быть объектом JSON")
        shot = dict(original)
        unknown = shot.keys() - {"id", "source_id", "start_s", "end_s", "duration_s", "purpose",
                                 "title", "audio", "fade_in_s", "fade_out_s"}
        if unknown:
            raise AnalysisError(f"Неизвестные поля сцены: {sorted(unknown)}")
        label = shot.get("id", "")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label) or label in seen:
            raise AnalysisError(f"Неверный или повторный id сцены: {label}")
        seen.add(label)
        item = material(shot["source_id"])
        if item["kind"] == "video":
            start = number(shot.get("start_s"), f"{label}.start_s")
            end = number(shot.get("end_s"), f"{label}.end_s")
            if not start < end <= item["duration_s"]:
                raise AnalysisError(f"{label}: диапазон выходит за исходник или пуст")
            duration = end - start
        else:
            start, end = None, None
            duration = number(shot.get("duration_s"), f"{label}.duration_s", 0.01)
        frames = round(duration * settings["fps"])
        if frames < 1 or abs(frames / settings["fps"] - duration) > 1e-6:
            raise AnalysisError(f"{label}: длительность должна содержать целое число кадров")
        evidence = [entry for entry in visual if entry["source_id"] == item["id"] and
                    (item["kind"] == "image" or (entry["start_s"] < end and entry["end_s"] > start))]
        if item["kind"] == "video":
            covered = start
            for entry in sorted(evidence, key=lambda e: e["start_s"]):
                if entry["start_s"] > covered + 1e-6:
                    break
                covered = max(covered, entry["end_s"])
            if covered < end - 1e-6:
                raise AnalysisError(f"{label}: диапазон не покрыт сохранённым анализом")
        elif not evidence:
            raise AnalysisError(f"{label}: нет описания фотографии")
        audio = dict(shot.get("audio", {"mode": "original" if item["has_audio"] else "silence"}))
        mode = audio.get("mode")
        if mode == "original":
            if not item["has_audio"]:
                raise AnalysisError(f"{label}: у исходника нет аудио")
            audio_item, audio_start = item, start
        elif mode == "source":
            audio_item = material(audio["source_id"])
            audio_start = number(audio.get("start_s"), f"{label}.audio.start_s")
            if not audio_item["has_audio"] or audio_start + duration > audio_item["duration_s"] + 1e-6:
                raise AnalysisError(f"{label}: неверный диапазон подложенного звука")
        elif mode == "silence":
            audio_item, audio_start = None, None
        else:
            raise AnalysisError(f"{label}: неизвестный режим звука {mode}")
        gain = number(audio.get("gain", 1.0), f"{label}.audio.gain")
        if gain > 4:
            raise AnalysisError(f"{label}: усиление звука ограничено значением 4")
        for fade in ("fade_in_s", "fade_out_s"):
            shot[fade] = number(shot.get(fade, 0), f"{label}.{fade}")
            if shot[fade] > duration:
                raise AnalysisError(f"{label}: наплыв длиннее сцены")
        if "title" in shot and (not isinstance(shot["title"], str) or len(shot["title"]) > 200):
            raise AnalysisError(f"{label}: титр должен быть строкой до 200 символов")
        if plan["schema_version"] == 2 and "title" in shot:
            raise AnalysisError("В формате 2 титры хранятся отдельно от сцен")
        resolved.append({**shot, "kind": item["kind"], "start_s": start, "end_s": end,
                         "duration_s": duration, "frames": frames, "media": item,
                         "timeline_start_s": cursor_frames / settings["fps"],
                         "timeline_end_s": (cursor_frames + frames) / settings["fps"],
                         "audio": {**audio, "gain": gain}, "audio_media": audio_item,
                         "audio_start_s": audio_start, "evidence": evidence})
        cursor_frames += frames
    titles = upgrade_plan(plan).get("titles", [])
    if not isinstance(titles, list):
        raise AnalysisError("titles должен быть списком")
    title_ids, title_errors = set(), []
    for title in titles:
        if not isinstance(title, dict):
            raise AnalysisError("Каждый титр должен быть объектом JSON")
        label = title.get("id", "")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label) or label in title_ids:
            raise AnalysisError("Неверный или повторный id титра")
        title_ids.add(label)
        if not isinstance(title.get("text"), str) or not 1 <= len(title["text"].strip()) <= 200:
            raise AnalysisError(f"{label}: нужен непустой текст до 200 символов")
        start = number(title.get("start_s"), f"{label}.start_s")
        end = number(title.get("end_s"), f"{label}.end_s")
        if end <= start:
            raise AnalysisError(f"{label}: конец титра должен быть позже начала")
        if end > cursor_frames / settings["fps"] + 1e-6:
            title_errors.append(f"{label}: титр выходит за конец фильма ({cursor_frames/settings['fps']:.3f} с)")
    ordered = sorted(titles, key=lambda t: t["start_s"])
    if any(a["end_s"] > b["start_s"] for a, b in zip(ordered, ordered[1:])):
        raise AnalysisError("Титры не должны пересекаться по времени")
    if title_errors and not allow_title_errors:
        raise AnalysisError("; ".join(title_errors))
    for shot in resolved:
        shot["overlays"] = [{"text": title["text"],
                              "start_s": round(max(title["start_s"], shot["timeline_start_s"]) - shot["timeline_start_s"], 6),
                              "end_s": round(min(title["end_s"], shot["timeline_end_s"]) - shot["timeline_start_s"], 6)}
                             for title in ordered if title["start_s"] < shot["timeline_end_s"]
                             and title["end_s"] > shot["timeline_start_s"]]
    return {"spec": plan, "path": str(path), "analysis": str(analysis), "source": str(source),
            "settings": settings, "shots": resolved, "frames": cursor_frames,
            "duration_s": cursor_frames / settings["fps"], "titles": ordered,
            "validation_errors": title_errors}


def run_ffmpeg(arguments: list, log: Path, cwd: Path) -> str:
    """Wait synchronously, terminate FFmpeg on Ctrl+C, retain diagnostics."""
    log.parent.mkdir(parents=True, exist_ok=True)
    check_pause()
    with log.open("wb") as stream:
        process = subprocess.Popen([str(a) for a in arguments], cwd=cwd, stdout=stream,
                                   stderr=subprocess.STDOUT)
        try:
            while True:
                try:
                    code = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    check_pause()
            check_pause()
        except BaseException:
            if process.poll() is None:
                process.terminate()
            process.wait()
            raise
    content = log.read_text(encoding="utf-8", errors="replace")
    if code:
        raise AnalysisError(f"FFmpeg: код {code}. Журнал: {log}\n{content[-4000:]}")
    return content


def fingerprint(item: dict | None):
    if item is None:
        return None
    return {k: item[k] for k in ("path", "size_bytes", "mtime_ns")}


def title_assets(cache: Path, title: str) -> tuple[str, str]:
    font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"
    if not font.is_file():
        raise AnalysisError(f"Для русских титров нужен шрифт {font}")
    content = font.read_bytes()
    font_name = hashlib.sha256(content).hexdigest()[:16] + ".ttf"
    assets = cache / "assets"
    assets.mkdir(exist_ok=True)
    if not (assets / font_name).is_file():
        temporary = assets / f"{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, assets / font_name)
    text_name = digest(title) + ".txt"
    atomic_text(assets / text_name, title)
    return "assets/" + font_name, "assets/" + text_name


def render_shot(shot: dict, settings: dict, cache: Path, ffmpeg_version: str) -> dict:
    check_pause()
    overlays = [{**overlay, "assets": title_assets(cache, overlay["text"])} for overlay in shot.get("overlays", [])]
    whole = len(overlays) == 1 and overlays[0]["start_s"] == 0 and overlays[0]["end_s"] == shot["duration_s"]
    # Preserve the original cache keys for untitled and whole-scene title fragments.
    title = overlays[0]["assets"] if whole else ({"timed": overlays} if overlays else None)
    recipe = {"render_version": RENDER_VERSION, "ffmpeg": ffmpeg_version,
              "settings": settings, "media": fingerprint(shot["media"]),
              "audio_media": fingerprint(shot["audio_media"]), "audio_start_s": shot["audio_start_s"],
              **{k: shot[k] for k in ("start_s", "duration_s", "audio", "fade_in_s", "fade_out_s")},
              "title": title}
    key = digest(recipe)
    target, manifest = cache / f"{key}.mkv", cache / f"{key}.json"
    if manifest.is_file() and target.is_file():
        try:
            value = load_json(manifest)
            if value.get("key") == key and value.get("size_bytes") == target.stat().st_size:
                return {**value, "cache_hit": True, "path": str(target)}
        except (ValueError, OSError, AttributeError):
            pass
    args = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    duration = shot["duration_s"]
    if shot["kind"] == "video":
        if settings["decode"] == "cuda":
            args += ["-hwaccel", "cuda"]
        args += ["-ss", shot["start_s"], "-t", duration, "-i", shot["media"]["path"]]
    else:
        args += ["-loop", "1", "-framerate", settings["fps"], "-i", shot["media"]["path"]]
    if shot["audio"]["mode"] == "original":
        audio_input = "0:a:0"
    elif shot["audio_media"] is not None:
        args += ["-ss", shot["audio_start_s"], "-t", duration, "-i", shot["audio_media"]["path"]]
        audio_input = "1:a:0"
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_input = "1:a:0"
    w, h, fps = (settings[k] for k in ("width", "height", "fps"))
    video_filter = (f"[0:v:0]setpts=PTS-STARTPTS,fps={fps},"
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                    f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={shot['frames']}")
    for overlay in overlays:
        asset = overlay["assets"]
        video_filter += (f",drawtext=fontfile={asset[0]}:textfile={asset[1]}:expansion=none:"
                         f"fontsize={max(16, round(h * 0.042))}:fontcolor=white:"
                         "x=(w-tw)/2:y=h-th-h*0.09:box=1:boxcolor=black@0.48:boxborderw=16")
        if not whole:
            video_filter += f":enable='gte(t,{overlay['start_s']})*lt(t,{overlay['end_s']})'"
    if shot["fade_in_s"]:
        video_filter += f",fade=t=in:st=0:d={shot['fade_in_s']}"
    if shot["fade_out_s"]:
        video_filter += f",fade=t=out:st={duration-shot['fade_out_s']}:d={shot['fade_out_s']}"
    fade = min(0.06, duration / 4)
    audio_filter = (f"[{audio_input}]asetpts=PTS-STARTPTS,aresample=48000,"
                    f"aformat=sample_fmts=s16:channel_layouts=stereo,highpass=f=65,"
                    f"volume={shot['audio']['gain']},apad,atrim=end_sample={round(duration*48000)},"
                    f"afade=t=in:d={fade},afade=t=out:st={duration-max(fade,shot['fade_out_s'])}:"
                    f"d={max(fade,shot['fade_out_s'])},asetpts=N/SR/TB[a]")
    args += ["-filter_complex_threads", "2", "-filter_complex", video_filter + "[v];" + audio_filter,
             "-map", "[v]", "-map", "[a]", "-map_metadata", "-1", "-map_chapters", "-1",
             "-c:v", settings["encoder"]]
    if settings["encoder"] == "h264_nvenc":
        args += ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    else:
        args += ["-preset", "fast", "-crf", "20", "-threads", "2"]
    temporary = cache / f"{key}.partial.mkv"
    args += ["-g", fps * 2, "-c:a", "pcm_s16le", "-t", duration, temporary]
    began = time.perf_counter()
    try:
        run_ffmpeg(args, cache / f"{key}.log", cache)
        metadata = probe(temporary)
        streams = metadata["streams"]
        if not any(s["codec_type"] == "video" for s in streams) or not any(s["codec_type"] == "audio" for s in streams):
            raise AnalysisError(f"Неполный фрагмент: {shot['id']}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    result = {"key": key, "size_bytes": target.stat().st_size, "duration_s": duration,
              "wall_time_s": round(time.perf_counter() - began, 3), "recipe": recipe}
    atomic_json(manifest, result)
    return {**result, "cache_hit": False, "path": str(target)}


def loudness_filter(log: str) -> tuple[str, dict]:
    matches = re.findall(r'\{\s*"input_i".*?\}', log, re.S)
    if not matches:
        raise AnalysisError("FFmpeg не вернул измерения громкости")
    measured = json.loads(matches[-1])
    if not all(math.isfinite(float(measured[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh")):
        return "anull", {"skipped": "нет измеримого звука", "measurement": measured}
    result = ("loudnorm=I=-18:TP=-1.5:LRA=11:linear=true:"
              f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
              f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
              f"offset={measured['target_offset']}")
    return result, measured


def write_review(plan: dict, output: Path, state: dict) -> None:
    spec = plan["spec"]
    escape = lambda text: html.escape(str(text))
    rows, text = [], [f"# {spec.get('title', 'Монтаж')}", "", spec.get("brief", ""), "",
                      f"Длительность: {timestamp(plan['duration_s'])}. Статус: {state['status']}.", "",
                      "[Посмотреть фильм и монтажный лист](watch.html)", ""]
    for paragraph in spec.get("story", []):
        text += [paragraph, ""]
    text += ["## Монтажный лист", ""]
    timeline = []
    for n, shot in enumerate(plan["shots"], 1):
        start, end = shot["timeline_start_s"], shot["timeline_end_s"]
        source_url = media_url(Path(shot["media"]["path"]), output)
        source_range = "фотография" if shot["kind"] == "image" else f"{timestamp(shot['start_s'])}–{timestamp(shot['end_s'])}"
        if shot["kind"] == "video":
            source_url += f"#t={shot['start_s']},{shot['end_s']}"
        mode = shot["audio"]["mode"]
        sound = "Исходный синхронный звук" if mode == "original" else "Тишина"
        if mode == "source":
            sound = f"Атмосфера {shot['audio_media']['id']} с {timestamp(shot['audio_start_s'])}; без синхронизации"
        text += [f"### {n:02}. {shot['id']} · {timestamp(start)}–{timestamp(end)}", "",
                 f"[{shot['source_id']} · {source_range}]({source_url})", "",
                 shot.get("purpose", ""), "", f"Звук: {sound}.", ""]
        for overlay in shot.get("overlays", []):
            text += ["Титр: " + overlay["text"].replace("\n", " / "), ""]
        rows.append(f'<tr><td><button data-seek="{start}">{n:02} · {timestamp(start)}</button></td>'
                    f'<td>{escape(shot.get("purpose", ""))}<br><small>{escape(sound)}</small></td>'
                    f'<td><a href="{escape(source_url)}">{escape(shot["source_id"])}<br>{escape(source_range)}</a></td></tr>')
        timeline.append({k: shot[k] for k in ("id", "source_id", "kind", "start_s", "end_s", "duration_s",
                                              "timeline_start_s", "timeline_end_s", "audio", "audio_start_s")}
                        | {"purpose": shot.get("purpose", ""), "evidence_keys": [e["key"] for e in shot["evidence"]]})
    text += ["## Титры на шкале фильма", ""]
    text += [f"{timestamp(t['start_s'])}–{timestamp(t['end_s'])}: {t['text']}" for t in plan["titles"]]
    text += [""]
    notes = spec.get("uncertainties", [])
    text += ["## Неточности и ограничения", "", *[f"- {note}" for note in notes], "",
             "## Сборка", "", f"Время запуска: {state.get('wall_time_s', 0):.1f} с. "
             f"Из кеша: {sum(s.get('cache_hit', False) for s in state['shots'])} из {len(plan['shots'])} сцен.", "",
             "Общая громкость измерена до кодирования и выровнена в два прохода: цель −18 LUFS, "
             "предел истинного пика −1,5 dBTP. Длительность и число кадров проверены автоматически. "
             "Художественное качество и слышимость речи проверяются локальным просмотром.", ""]
    page = ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(spec.get("title", "Монтаж"))}</title><style>'
            'body{font:16px system-ui;background:#171a1f;color:#edf0f4;max-width:1100px;margin:24px auto;padding:0 20px}'
            'video{width:100%;max-height:72vh;background:#000}table{border-collapse:collapse;width:100%}'
            'td{padding:12px 8px;border-bottom:1px solid #41464d;vertical-align:top}small{color:#b8c1ce}'
            'a{color:#9ac9ff}button{cursor:pointer;background:#303b4b;color:white;border:0;border-radius:5px;padding:9px;white-space:nowrap}'
            '</style>'
            f'<h1>{escape(spec.get("title", "Монтаж"))}</h1><p>{escape(spec.get("brief", ""))}</p>'
            f'<p>{timestamp(plan["duration_s"])} · {plan["settings"]["width"]}×{plan["settings"]["height"]} · '
            f'{plan["settings"]["fps"]} кадр/с</p><video id="film" controls preload="metadata" src="film.mp4"></video>'
            '<h2>Монтажный лист</h2><p>Кнопка времени перематывает фильм. Ссылка справа открывает исходный диапазон.</p><table>'
            + "".join(rows) + '</table><h2>Неточности и ограничения</h2><ul>'
            + "".join(f'<li>{escape(note)}</li>' for note in notes) + '</ul>'
            '<script>const v=document.getElementById("film");document.querySelectorAll("[data-seek]").forEach(b=>'
            'b.addEventListener("click",()=>{v.currentTime=Number(b.dataset.seek);v.play();v.scrollIntoView({behavior:"smooth",block:"center"})}));</script></html>')
    atomic_json(output / "timeline.json", {"duration_s": plan["duration_s"], "shots": timeline, "titles": plan["titles"]})
    atomic_text(output / "montage.md", "\n".join(text))
    atomic_text(output / "watch.html", page)


def render(plan_path: Path, output: Path, *, cache: Path | None = None) -> dict:
    plan = load_plan(plan_path)
    output = output.resolve()
    cache = (cache or RUNS / "render-cache").resolve()
    source, analysis = Path(plan["source"]), Path(plan["analysis"])
    for folder in (output, cache):
        if folder == source or source in folder.parents or folder == analysis or analysis in folder.parents:
            raise AnalysisError("Сборка и кеш должны находиться вне исходников и каталога анализа")
    if output == cache or output in cache.parents or cache in output.parents:
        raise AnalysisError("Папка результата и общий кеш должны быть отдельными")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    state = {"status": "running", "started_at": now(), "plan": str(plan_path.resolve()),
             "plan_digest": digest(plan["spec"]), "settings": plan["settings"],
             "duration_s": plan["duration_s"], "shots": [], "render_version": RENDER_VERSION,
             "ffmpeg": tool_version("ffmpeg")}
    began = time.perf_counter()
    temporary = output / "film.partial.mp4"
    # A snapshot must remain runnable after moving from edits/ to the result directory.
    atomic_json(output / "plan.json", {**plan["spec"], "analysis": plan["analysis"]})
    atomic_json(output / "render.json", state)
    try:
        for index, shot in enumerate(plan["shots"], 1):
            check_pause()
            print(f"Render {index}/{len(plan['shots'])}: {shot['id']} ({shot['duration_s']:.2f}s)", flush=True)
            result = render_shot(shot, plan["settings"], cache, state["ffmpeg"])
            state["shots"].append({"id": shot["id"], **result})
            atomic_json(output / "render.json", state)
        concat = cache / (digest([s["key"] for s in state["shots"]]) + ".ffconcat")
        atomic_text(concat, "ffconcat version 1.0\n" + "".join(
            f"file {s['key']}.mkv\nduration {s['duration_s']:.9f}\n" for s in state["shots"]))
        base = [executable("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", concat]
        print("Measure and normalize soundtrack; assemble MP4", flush=True)
        measurement = run_ffmpeg(base + ["-vn", "-af", "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json",
                                        "-f", "null", "-"], output / "loudness.log", ROOT)
        audio_filter, state["loudness"] = loudness_filter(measurement)
        run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "-1",
                          "-c:v", "copy", "-af", audio_filter, "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                          "-t", plan["duration_s"], "-movflags", "+faststart", temporary], output / "assemble.log", ROOT)
        metadata = probe(temporary)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        audio = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
        if (int(video.get("nb_frames", 0)) != plan["frames"] or
            (video["width"], video["height"]) != (plan["settings"]["width"], plan["settings"]["height"]) or
            abs(float(metadata["format"]["duration"]) - plan["duration_s"]) > 1 / plan["settings"]["fps"] + 0.02 or
            abs(float(audio.get("duration", 0)) - plan["duration_s"]) > 0.05):
            raise AnalysisError("Итоговый MP4 не соответствует таймлайну; см. render.json и assemble.log")
        # Full decode validates all packets, including cuts; no media leaves the PC.
        run_ffmpeg([executable("ffmpeg"), "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-i", temporary,
                    "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], output / "decode-check.log", ROOT)
        state["validation"] = {"frames": int(video["nb_frames"]), "video_duration_s": float(video["duration"]),
                               "audio_duration_s": float(audio["duration"]), "full_decode": "passed"}
        state["size_bytes"] = temporary.stat().st_size
        os.replace(temporary, output / "film.mp4")
        state["status"] = "complete"
    except BaseException as exc:
        state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        state["error"] = str(exc) or type(exc).__name__
        raise
    finally:
        temporary.unlink(missing_ok=True)
        state["wall_time_s"] = round(time.perf_counter() - began, 3)
        atomic_json(output / "render.json", state)
    write_review(plan, output, state)
    return state
