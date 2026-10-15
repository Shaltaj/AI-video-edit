from __future__ import annotations

import html
import json
import os
from pathlib import Path
from urllib.parse import quote

from .common import atomic_json, atomic_text, digest, load_json, timestamp


def media_url(path: Path, folder: Path) -> str:
    try:
        return quote(os.path.relpath(path, folder).replace("\\", "/"), safe="/:")
    except ValueError:
        # Windows sources can live on D: while reports live on C:.
        return path.as_uri()


def report(folder: Path) -> Path:
    from .pipeline import result_references

    run = load_json(folder / "run.json")
    visual, transcripts = result_references(run)
    media_by_id = {item["id"]: item for item in run.get("media", [])}
    elapsed = f"{run['wall_time_s']:.2f} с" if "wall_time_s" in run else "выполняется"
    chunks = ["# Локальный анализ материалов", "", f"Статус: **{run['status']}**. Профиль: `{run['profile']}`.",
              f"Время текущего запуска: {elapsed}.", "",
              f"Готово описаний: {len(visual)} из {len(run.get('visual_jobs', []))}; "
              f"расшифровок файлов: {len(transcripts)} из {len(run.get('asr_jobs', []))}.", "",
              "[Просмотр сцен и исходных временных диапазонов](preview.html)", "",
              "Таймкоды относятся к исходникам. Описания и сомнения получены локальными моделями; "
              "содержательная проверка выполняется по исходному материалу.", ""]
    sections = []
    for item in visual:
        source_id = item["source_id"]
        media = media_by_id[source_id]
        path = Path(media["source_root"]) / media["relative_path"]
        relative = media_url(path, folder)
        anchor = "scene-" + digest(item["key"])[:16]
        if item["kind"] == "video":
            start, end = item["start_s"], item["end_s"]
            label = f"{source_id} — {timestamp(start)}–{timestamp(end)}"
            player = (f'<video controls preload="none" data-start="{start}" data-end="{end}" '
                      f'src="{html.escape(relative)}#t={start},{end}"></video>')
        else:
            label = source_id
            player = f'<img loading="lazy" src="{html.escape(relative)}" alt="{html.escape(source_id)}">'
        chunks += [f"## [{label}](preview.html#{anchor})", "", item["description"], ""]
        for uncertainty in item["uncertainties"]:
            chunks += [f"Сомнения: {uncertainty}", ""]
        sections.append(f'<section id="{anchor}"><h2>{html.escape(label)}</h2>{player}'
                        f'<p>{html.escape(item["description"])}</p>'
                        + "".join(f'<p><b>Сомнения:</b> {html.escape(text)}</p>'
                                  for text in item["uncertainties"]) + '</section>')
    for transcript in transcripts:
        chunks += [f"## Речь — {transcript['source_id']}", ""]
        if not transcript["segments"]:
            chunks += ["Речь не распознана после фильтра речевой активности.", ""]
        for segment in transcript["segments"]:
            chunks += [f"{timestamp(segment['start_s'])}–{timestamp(segment['end_s'])}: {segment['text']}", ""]
            for uncertainty in segment["uncertainties"]:
                chunks += [f"Сомнения: {uncertainty}", ""]
    for media in run.get("media", []):
        if media["kind"] == "video" and not media["has_audio"]:
            chunks += [f"Аудиодорожка отсутствует: `{media['id']}`.", ""]
    if run["errors"]:
        chunks += ["## Ошибки обработки", ""]
        for error in run["errors"]:
            chunks += [error, ""]
    chunks += ["## Модели и измерения", "", "```json",
               json.dumps({"models": run.get("models", {}), "settings": run["settings"],
                           "phases": run.get("phases", []), "versions": run["versions"]},
                          ensure_ascii=False, indent=2), "```", "",
               "Память GPU измеряется по всему устройству, включая рабочий стол и другие приложения. "
               "При повторном запуске кешированные операции сохраняют измерения исходного вычисления.", ""]
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Локальный просмотр анализа</title><style>
body{font:16px system-ui;max-width:960px;margin:32px auto;padding:0 20px;background:#16191d;color:#eceff3}
section{margin:36px 0;padding-bottom:28px;border-bottom:1px solid #444}h2{font-size:18px;overflow-wrap:anywhere}
video,img{max-width:100%;max-height:65vh}p{white-space:pre-wrap;line-height:1.6}
</style><h1>Локальный просмотр анализа</h1><p>Таймкоды относятся к исходным файлам.</p>'''
    page += "\n".join(sections)
    page += '''<script>document.querySelectorAll('video').forEach(v=>{
v.addEventListener('loadedmetadata',()=>{v.currentTime=Number(v.dataset.start)});
v.addEventListener('play',()=>{if(v.currentTime>=Number(v.dataset.end)||v.currentTime<Number(v.dataset.start))v.currentTime=Number(v.dataset.start)});
v.addEventListener('timeupdate',()=>{if(v.currentTime>=Number(v.dataset.end))v.pause()});
});</script></html>'''
    atomic_text(folder / "analysis.md", "\n".join(chunks))
    atomic_text(folder / "preview.html", page)
    atomic_text(folder / "segments.jsonl", "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in visual))
    atomic_json(folder / "transcripts.json", transcripts)
    atomic_json(folder / "metrics.json", {"status": run["status"], "wall_time_s": run.get("wall_time_s"),
                "cache_stats": run.get("cache_stats", {}),
                "phases": run.get("phases", []), "preparation": run.get("preparation", []),
                "visual": [{"key": item["key"], "source_id": item["source_id"], "metrics": item["metrics"]}
                           for item in visual],
                "asr": [{"key": item["key"], "source_id": item["source_id"], "metrics": item["metrics"]}
                        for item in transcripts]})
    return folder / "analysis.md"
