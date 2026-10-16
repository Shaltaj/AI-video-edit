from __future__ import annotations

import argparse
import ctypes
import importlib.util
import math
import os
from pathlib import Path
import sys
import traceback

from .common import (AnalysisError, ROOT, atomic_json, cached_result, complete_result,
                     load_json, now, offline_environment, versions, check_pause, progress)
from .monitor import GPUMonitor

_DLL_HANDLES = []
VISUAL_PROMPT = """Опиши только то, что можно увидеть на приложенных кадрах.
Для последовательности расскажи, как меняется действие: начало, попытка, движение, результат,
если они видны. Не приписывай людям имена, машинам владельцев, событиям причины или исход,
которых нельзя установить по кадрам. Не придумывай речь и звук.
Ответь по-русски кратко, 2–4 предложения, в двух разделах:
НАБЛЮДЕНИЕ: описание видимого действия и значимых объектов.
СОМНЕНИЯ: что нельзя определить уверенно; если сомнений нет, напиши «не отмечены».
"""


def configure_cuda() -> Path:
    specification = importlib.util.find_spec("torch")
    if specification is None or specification.origin is None:
        raise AnalysisError("PyTorch не установлен")
    library = Path(specification.origin).parent / "lib"
    if os.name == "nt":
        _DLL_HANDLES.append(os.add_dll_directory(str(library)))
        os.environ["PATH"] = str(library) + os.pathsep + os.environ.get("PATH", "")
        for name in ("cublas64_12.dll", "cudnn64_9.dll"):
            path = library / name
            if not path.is_file():
                raise AnalysisError(f"Отсутствует локальная CUDA библиотека {path}")
            _DLL_HANDLES.append(ctypes.WinDLL(str(path)))
    return library


def doctor(job: dict) -> dict:
    library = configure_cuda()
    import torch
    import bitsandbytes as bnb
    import ctranslate2

    if not torch.cuda.is_available():
        raise AnalysisError("CUDA недоступна для PyTorch; переход на CPU запрещён")
    with GPUMonitor() as monitor:
        with torch.inference_mode():
            tensor = torch.ones((128, 128), device="cuda", dtype=torch.float16)
            answer = (tensor @ tensor).float().sum().item()
            if not math.isfinite(answer) or answer != 2097152.0:
                raise AnalysisError("Тест матричного умножения на GPU не пройден")
            layer = bnb.nn.Linear4bit(64, 64, bias=False, compute_dtype=torch.float16,
                                     quant_type="nf4").to("cuda")
            quantized = layer(torch.ones((1, 64), device="cuda", dtype=torch.float16))
            torch.cuda.synchronize()
            if not torch.isfinite(quantized).all().item():
                raise AnalysisError("Тест NF4 вернул неконечные значения")
        count = ctranslate2.get_cuda_device_count()
        supported = sorted(ctranslate2.get_supported_compute_types("cuda", 0))
        if count < 1 or "int8_float16" not in supported:
            raise AnalysisError("CTranslate2 не поддерживает CUDA int8_float16")
    return {"status": "complete", "gpu": torch.cuda.get_device_name(0),
            "cuda_runtime": torch.version.cuda, "torch_gpu_test": answer,
            "nf4_gpu_test": "passed", "ctranslate2_cuda_devices": count,
            "ctranslate2_compute_types": supported, "cuda_library_directory": str(library),
            "versions": versions(), "metrics": monitor.result()}


def asr_jobs(job: dict) -> None:
    configure_cuda()
    from faster_whisper import WhisperModel

    model_info = job["model"]
    cache = Path(job["cache"])
    with GPUMonitor() as phase_monitor:
        model = WhisperModel(str(ROOT / model_info["path"]), device="cuda", device_index=0,
                             compute_type="int8_float16", local_files_only=True,
                             cpu_threads=4, num_workers=1)
        for number, item in enumerate(job["items"], 1):
            check_pause()
            if cached_result(cache, item["key"]):
                continue
            print(f"[asr {number}/{len(job['items'])}] {item['source_id']}", flush=True)
            with GPUMonitor() as monitor:
                segments, info = model.transcribe(item["audio_path"], language="ru",
                                                  beam_size=5, vad_filter=True,
                                                  word_timestamps=True,
                                                  condition_on_previous_text=False)
                raw, normalized = [], []
                for segment in segments:
                    words = [{"start": word.start, "end": word.end,
                              "word": word.word, "probability": word.probability}
                             for word in (segment.words or [])]
                    original = {"start": segment.start, "end": segment.end,
                                "text": segment.text, "avg_logprob": segment.avg_logprob,
                                "no_speech_prob": segment.no_speech_prob,
                                "compression_ratio": segment.compression_ratio, "words": words}
                    raw.append(original)
                    duration = item["duration_s"]
                    start, end = max(0.0, segment.start), min(duration, segment.end)
                    if end <= start or not segment.text.strip():
                        continue
                    uncertainties = []
                    if segment.avg_logprob < -0.8 or segment.no_speech_prob > 0.5:
                        uncertainties.append("Низкая уверенность распознавания речи; проверить по звуку.")
                    if start != segment.start or end != segment.end:
                        uncertainties.append("Граница распознавания ограничена длительностью исходника.")
                    bounded_words = [{**word, "start": max(start, word["start"]),
                                      "end": min(end, word["end"])} for word in words
                                     if min(end, word["end"]) > max(start, word["start"])]
                    normalized.append({"source_id": item["source_id"], "start_s": start,
                                       "end_s": end, "text": segment.text.strip(),
                                       "uncertainties": uncertainties, "words": bounded_words})
            complete_result(cache, item["key"], {"kind": "asr", "source_id": item["source_id"],
                            "duration_s": item["duration_s"], "segments": normalized,
                            "raw_segments": raw, "language": info.language,
                            "language_probability": info.language_probability,
                            "metrics": monitor.result(), "model": model_info,
                            "settings": job["settings"], "created_at": now()})
            progress("Речь", job.get("cached_count", 0)+number, job.get("total", len(job["items"])), job.get("cached_count", 0))
    atomic_json(Path(job["phase_output"]), {"kind": "asr", "metrics": phase_monitor.result(),
                                           "versions": versions(), "model": model_info})


def visual_jobs(job: dict) -> None:
    import torch
    from .images import open_photo
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen3VLForConditionalGeneration

    if not torch.cuda.is_available():
        raise AnalysisError("CUDA недоступна; переход визуальной модели на CPU запрещён")
    model_info = job["model"]
    model_path = str(ROOT / model_info["path"])
    cache = Path(job["cache"])
    with GPUMonitor() as phase_monitor:
        quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                          bnb_4bit_compute_dtype=torch.float16,
                                          bnb_4bit_use_double_quant=True)
        processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
        model = Qwen3VLForConditionalGeneration.from_pretrained(
            model_path, local_files_only=True, quantization_config=quantization,
            device_map={"": 0}, torch_dtype=torch.float16, attn_implementation="sdpa").eval()
        for number, item in enumerate(job["items"], 1):
            check_pause()
            if cached_result(cache, item["key"]):
                continue
            print(f"[visual {number}/{len(job['items'])}] {item['source_id']} "
                  f"{item.get('start_s')}..{item.get('end_s')}", flush=True)
            with GPUMonitor() as monitor:
                images, content = [], [{"type": "text", "text": VISUAL_PROMPT}]
                for frame in item["frames"]:
                    with open_photo(frame["path"]) as image:
                        image.thumbnail((job["settings"]["max_side"], job["settings"]["max_side"]))
                        images.append(image.copy())
                    if frame.get("source_time_s") is not None:
                        content.append({"type": "text", "text": f"Кадр исходника: {frame['source_time_s']:.3f} с."})
                    content.append({"type": "image", "image": images[-1]})
                messages = [{"role": "user", "content": content}]
                text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                inputs = processor(text=[text], images=images, return_tensors="pt", padding=True)
                inputs = inputs.to("cuda")
                with torch.inference_mode():
                    generated = model.generate(**inputs, max_new_tokens=job["settings"]["max_new_tokens"],
                                               do_sample=False)
                reply = processor.batch_decode(generated[:, inputs["input_ids"].shape[1]:],
                                               skip_special_tokens=True)[0].strip()
                torch.cuda.synchronize()
                if not reply:
                    raise AnalysisError(f"Модель вернула пустой ответ: {item['source_id']}")
                split = reply.upper().find("СОМНЕНИЯ:")
                description = reply[:split].strip() if split >= 0 else reply
                description = description.removeprefix("НАБЛЮДЕНИЕ:").strip()
                uncertainty = reply[split + len("СОМНЕНИЯ:"):].strip() if split >= 0 else ""
                uncertainties = [uncertainty] if uncertainty else ["Модель не выделила раздел сомнений; проверить ответ."]
                if generated.shape[1] - inputs["input_ids"].shape[1] >= job["settings"]["max_new_tokens"]:
                    uncertainties.append("Ответ достиг ограничения длины и мог быть обрезан.")
                del inputs, generated
                for image in images:
                    image.close()
            complete_result(cache, item["key"], {"kind": item["kind"], "source_id": item["source_id"],
                            "start_s": item.get("start_s"), "end_s": item.get("end_s"),
                            "description": description, "uncertainties": uncertainties,
                            "raw_response": reply,
                            "evidence_times_s": [frame.get("source_time_s") for frame in item["frames"]],
                            "metrics": monitor.result(), "model": model_info,
                            "settings": job["settings"], "created_at": now()})
            progress("Описание изображения", job.get("cached_count", 0)+number, job.get("total", len(job["items"])), job.get("cached_count", 0))
    atomic_json(Path(job["phase_output"]), {"kind": "visual", "metrics": phase_monitor.result(),
                                           "versions": versions(), "model": model_info})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    job = load_json(args.job)
    os.environ.update(offline_environment())
    try:
        if job["mode"] == "doctor":
            atomic_json(Path(job["output"]), doctor(job))
        elif job["mode"] == "asr":
            asr_jobs(job)
        else:
            visual_jobs(job)
        return 0
    except Exception as exc:
        error = {"status": "error", "mode": job["mode"], "error": str(exc),
                 "oom": "out of memory" in str(exc).lower(), "traceback": traceback.format_exc(),
                 "settings": job.get("settings"), "model": job.get("model"), "versions": versions(),
                 "created_at": now()}
        atomic_json(Path(job["error_output"]), error)
        print(error["traceback"], file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
