"""Frame-based boundaries and bounded, resumable composition from originals."""
from __future__ import annotations

import os
from pathlib import Path
import time

from .common import AnalysisError, ROOT, atomic_json, digest, executable, load_json, tool_version

EFFECTS = {"crossfade": "fade", "fade_black": "fadeblack"}
SECTION_VERSION = 1


def resolve_timeline(spec: dict, shots: list[dict], fps: int):
    from .render import number
    raw = spec.get("transitions", [])
    if not isinstance(raw, list):
        raise AnalysisError("transitions должен быть списком")
    pairs = {(a["id"], b["id"]): i for i, (a, b) in enumerate(zip(shots, shots[1:]))}
    edges, errors = {}, []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"from_shot_id", "to_shot_id", "type", "duration_s"}:
            raise AnalysisError("Переход: нужны from_shot_id, to_shot_id, type, duration_s")
        pair = (entry["from_shot_id"], entry["to_shot_id"])
        if not all(isinstance(v, str) for v in pair) or pair not in pairs or pair in edges:
            raise AnalysisError("Переход должен однократно связывать соседние сцены")
        if not isinstance(entry["type"], str) or entry["type"] not in EFFECTS:
            raise AnalysisError("Неизвестный тип перехода")
        duration = number(entry["duration_s"], "Длительность перехода")
        frames = round(duration * fps)
        if frames < 1 or abs(frames / fps - duration) > 1e-6:
            raise AnalysisError("Длительность перехода должна содержать целое положительное число кадров")
        edges[pair] = {**entry, "frames": frames}
    cursor, result = 0, []
    for i, shot in enumerate(shots):
        before = edges.get((shots[i - 1]["id"], shot["id"]), {}) if i else {}
        after = edges.get((shot["id"], shots[i + 1]["id"]), {}) if i + 1 < len(shots) else {}
        incoming, outgoing = before.get("frames", 0), after.get("frames", 0)
        if incoming + outgoing >= shot["frames"]:
            errors.append(f"{shot['id']}: переходы слишком длинные; оставьте хотя бы один самостоятельный кадр")
        shot.update(timeline_start_frame=cursor, timeline_end_frame=cursor + shot["frames"],
                    timeline_start_s=cursor / fps, timeline_end_s=(cursor + shot["frames"]) / fps,
                    incoming_frames=incoming, outgoing_frames=outgoing)
        cursor += shot["frames"]
        if after:
            result.append({**after, "start_frame": cursor - outgoing, "end_frame": cursor,
                           "start_s": (cursor - outgoing) / fps, "end_s": cursor / fps})
        cursor -= outgoing
    return result, cursor, errors


def overlays(titles, start, end, fps):
    return [{"text": t["text"], "start_s": round(max(t["start_s"], start / fps) - start / fps, 6),
             "end_s": round(min(t["end_s"], end / fps) - start / fps, 6)}
            for t in titles if t["start_s"] < end / fps and t["end_s"] > start / fps]


def sections(plan: dict, start_frame=0, end_frame=None) -> list[dict]:
    """Partition a valid timeline: body, overlap, body; no three-way overlaps."""
    end_frame = plan["frames"] if end_frame is None else end_frame
    fps, result = plan["settings"]["fps"], []
    for i, shot in enumerate(plan["shots"]):
        start = shot["timeline_start_frame"] + shot["incoming_frames"]
        end = shot["timeline_end_frame"] - shot["outgoing_frames"]
        lo, hi = max(start, start_frame), min(end, end_frame)
        if lo < hi:
            result.append({"id": shot["id"], "section_kind": "body", "start_frame": lo,
                           "frames": hi - lo, "parts": [{"shot": shot,
                               "offset_frames": lo - shot["timeline_start_frame"]}]})
        if shot["outgoing_frames"]:
            start, end = end, shot["timeline_end_frame"]
            if start < end_frame and end > start_frame:
                if start < start_frame or end > end_frame:
                    raise AnalysisError("Просмотр должен включать весь переход")
                following = plan["shots"][i + 1]
                edge = next(t for t in plan["transitions"] if t["from_shot_id"] == shot["id"])
                result.append({"id": shot["id"] + "--" + following["id"], "section_kind": "transition",
                               "type": edge["type"], "start_frame": start, "frames": end - start,
                               "parts": [{"shot": shot, "offset_frames": shot["frames"] - (end - start)},
                                         {"shot": following, "offset_frames": 0}]})
    for piece in result:
        piece["duration_s"] = piece["frames"] / fps
        piece["overlays"] = overlays(plan["titles"], piece["start_frame"], piece["start_frame"] + piece["frames"], fps)
    return result


def recipe(piece, settings, ffmpeg):
    from .render import fingerprint
    return {"section_version": SECTION_VERSION, "ffmpeg": ffmpeg, "settings": settings,
            "frames": piece["frames"], "effect": piece.get("type"), "overlays": piece["overlays"],
            "parts": [{"media": fingerprint(p["shot"]["media"]),
                       "audio_media": fingerprint(p["shot"]["audio_media"]),
                       **{k: p["shot"][k] for k in ("start_s", "duration_s", "audio_start_s", "audio", "fade_in_s", "fade_out_s")},
                       "offset_frames": p["offset_frames"],
                       "cut_in": not p["shot"]["incoming_frames"],
                       "cut_out": not p["shot"]["outgoing_frames"]} for p in piece["parts"]]}


def render_section(piece, settings, cache, ffmpeg):
    from .render import check_pause, render_shot, run_ffmpeg, title_assets
    from .media import probe
    check_pause()
    cache.mkdir(parents=True, exist_ok=True)
    # Preserve the old recipe and bitstream for whole scenes without overlaps.
    if len(piece["parts"]) == 1:
        part = piece["parts"][0]
        if part["offset_frames"] == 0 and piece["frames"] == part["shot"]["frames"]:
            return render_shot({**part["shot"], "overlays": piece["overlays"]}, settings, cache, ffmpeg)
    spec = recipe(piece, settings, ffmpeg)
    key = digest(spec)
    target, manifest = cache / f"{key}.mkv", cache / f"{key}.json"
    if target.is_file() and manifest.is_file():
        try:
            previous = load_json(manifest)
            if previous.get("key") == key and previous.get("size_bytes") == target.stat().st_size:
                return {**previous, "cache_hit": True, "path": str(target)}
        except (OSError, ValueError, AttributeError):
            pass
    args = [executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    graph, input_index = [], 0
    w, h, fps = (settings[k] for k in ("width", "height", "fps"))
    duration = piece["duration_s"]
    for n, part in enumerate(piece["parts"]):
        shot, offset = part["shot"], part["offset_frames"] / fps
        if shot["kind"] == "video":
            if settings["decode"] == "cuda":
                args += ["-hwaccel", "cuda"]
            args += ["-ss", shot["start_s"] + offset, "-t", duration, "-i", shot["media"]["path"]]
        else:
            args += ["-loop", "1", "-framerate", fps, "-i", shot["media"]["path"]]
        vf = (f"[{input_index}:v]setpts=PTS-STARTPTS,fps={fps},"
              f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
              f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={piece['frames']},"
              f"settb=1/{fps},setpts=PTS+{part['offset_frames']}")
        if shot["fade_in_s"]:
            vf += f",fade=t=in:st=0:d={shot['fade_in_s']}"
        if shot["fade_out_s"]:
            vf += f",fade=t=out:st={shot['duration_s']-shot['fade_out_s']}:d={shot['fade_out_s']}"
        graph.append(vf + f",setpts=PTS-STARTPTS[v{n}]")
        input_index += 1
        # A short lead-in preserves high-pass filter state at internal section boundaries.
        warmup = min(offset, 0.1) if shot["audio_media"] else 0
        if shot["audio_media"]:
            args += ["-ss", shot["audio_start_s"] + offset - warmup, "-t", duration + warmup,
                     "-i", shot["audio_media"]["path"]]
        else:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        af = (f"[{input_index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
              f"aformat=sample_fmts=s16:channel_layouts=stereo,highpass=f=65,volume={shot['audio']['gain']},"
              f"apad,atrim=start_sample={round(warmup*48000)}:end_sample={round((warmup+duration)*48000)},"
              f"asetpts=N/SR/TB+{offset}/TB")
        fade = min(0.06, shot["duration_s"] / 4)
        if not shot["incoming_frames"]:
            af += f",afade=t=in:st=0:d={fade}"
        fade_out = max(shot["fade_out_s"], fade if not shot["outgoing_frames"] else 0)
        if fade_out:
            af += f",afade=t=out:st={shot['duration_s']-fade_out}:d={fade_out}"
        graph.append(af + f",asetpts=N/SR/TB[a{n}]")
        input_index += 1
    if len(piece["parts"]) == 2:
        graph += [f"[v0][v1]xfade=transition={EFFECTS[piece['type']]}:duration={duration}:offset=0[image]",
                  f"[a0][a1]acrossfade=ns={round(duration*48000)}:c1=tri:c2=tri[sound]"]
    else:
        graph += ["[v0]null[image]", "[a0]anull[sound]"]
    vf = f"[image]trim=end_frame={piece['frames']},setpts=PTS-STARTPTS,format=yuv420p"
    for title in piece["overlays"]:
        font, text = title_assets(cache, title["text"])
        vf += (f",drawtext=fontfile={font}:textfile={text}:expansion=none:fontsize={max(16, round(h*.042))}:"
               f"fontcolor=white:x=(w-tw)/2:y=h-th-h*0.09:box=1:boxcolor=black@0.48:boxborderw=16:"
               f"enable='gte(t,{title['start_s']})*lt(t,{title['end_s']})'")
    graph += [vf + "[v]", f"[sound]atrim=end_sample={round(duration*48000)},asetpts=N/SR/TB[a]"]
    args += ["-filter_complex_threads", "2", "-filter_complex", ";".join(graph),
             "-map", "[v]", "-map", "[a]", "-map_metadata", "-1", "-map_chapters", "-1",
             "-c:v", settings["encoder"]]
    args += (["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
             if settings["encoder"] == "h264_nvenc" else ["-preset", "fast", "-crf", "20", "-threads", "2"])
    temporary = target.with_suffix(".partial.mkv")
    args += ["-g", fps * 2, "-c:a", "pcm_s16le", "-t", duration, temporary]
    began = time.perf_counter()
    try:
        run_ffmpeg(args, cache / f"{key}.log", cache)
        streams = probe(temporary)["streams"]
        if not all(any(s["codec_type"] == kind for s in streams) for kind in ("video", "audio")):
            raise AnalysisError("Неполный участок монтажа")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    result = {"key": key, "size_bytes": target.stat().st_size, "duration_s": duration,
              "wall_time_s": round(time.perf_counter() - began, 3), "recipe": spec}
    atomic_json(manifest, result)
    return {**result, "cache_hit": False, "path": str(target)}


def preview_spec(plan, from_id, to_id):
    shots = plan["shots"]
    index = next((i for i in range(len(shots)-1) if (shots[i]["id"], shots[i+1]["id"]) == (from_id, to_id)), None)
    if index is None:
        raise AnalysisError("Для просмотра выберите соседние сцены")
    a, b = shots[index:index+2]
    fps = plan["settings"]["fps"]
    overlap = a["outgoing_frames"]
    start = a["timeline_end_frame"] - overlap - min(fps, a["frames"] - a["incoming_frames"] - overlap)
    end = a["timeline_end_frame"] + min(fps, b["frames"] - overlap - b["outgoing_frames"])
    settings = dict(plan["settings"])
    scale = min(1, 1280/settings["width"], 720/settings["height"])
    for k in ("width", "height"):
        settings[k] = max(64, int(settings[k]*scale)//2*2)
    pieces = sections(plan, start, end)
    version = tool_version("ffmpeg")
    key = digest([recipe(p, settings, version) for p in pieces])
    return {"key": key, "settings": settings, "pieces": pieces, "ffmpeg": version,
            "frames": end - start, "duration_s": (end-start)/fps, "start_s": start/fps}


def render_preview(plan, from_id, to_id, target: Path, cache: Path, progress: Path):
    from .render import run_ffmpeg
    from .common import atomic_text
    from .media import probe
    info = preview_spec(plan, from_id, to_id)
    results = []
    for piece in info["pieces"]:
        results.append(render_section(piece, info["settings"], cache, info["ffmpeg"]))
        atomic_json(progress, {"done": len(results), "total": len(info["pieces"]),
                               "cached": sum(r["cache_hit"] for r in results), "stage": "просмотр стыка"})
    concat = cache / (info["key"] + ".ffconcat")
    atomic_text(concat, "ffconcat version 1.0\n" + "".join(
        f"file {r['key']}.mkv\nduration {r['duration_s']:.9f}\n" for r in results))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".partial.mp4")
    try:
        run_ffmpeg([executable("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                    "-f", "concat", "-safe", "1", "-i", concat, "-map", "0:v:0", "-map", "0:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", info["duration_s"],
                    "-map_metadata", "-1", "-movflags", "+faststart", temporary], progress.parent / "preview.log", ROOT)
        metadata = probe(temporary)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        audio = next(s for s in metadata["streams"] if s["codec_type"] == "audio")
        if int(video.get("nb_frames", 0)) != info["frames"] or abs(float(audio.get("duration", 0))-info["duration_s"]) > .05:
            raise AnalysisError("Просмотр стыка не соответствует таймлайну")
        run_ffmpeg([executable("ffmpeg"), "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-i", temporary,
                    "-f", "null", "-"], progress.parent / "decode-check.log", ROOT)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"frames": info["frames"], "duration_s": info["duration_s"], "start_s": info["start_s"],
            "sections": len(results), "cached_sections": sum(r["cache_hit"] for r in results), "full_decode": "passed"}
