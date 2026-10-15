from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

from .common import AnalysisError, DEFAULT_SOURCE, RUNS, atomic_json


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Нужно положительное конечное число")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(prog="aivideo", description="Локальный анализ фото и видео")
    commands = parser.add_subparsers(dest="command", required=True)
    diagnostic = commands.add_parser("doctor", help="Проверка инструментов, моделей и GPU")
    diagnostic.add_argument("--output", type=Path, default=RUNS / "doctor")
    diagnostic.add_argument("--skip-models", action="store_true", help="Проверить окружение до загрузки весов")
    inventory = commands.add_parser("scan", help="Каталог исходников")
    inventory.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    inventory.add_argument("--output", type=Path, default=RUNS / "media-index.json")
    analysis = commands.add_parser("analyze", help="Локальное распознавание")
    analysis.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    analysis.add_argument("--profile", choices=("smoke", "sample"), default="smoke")
    analysis.add_argument("--output", type=Path)
    analysis.add_argument("--window-s", type=positive_number, default=10.0)
    analysis.add_argument("--fps", type=positive_number, default=2.0)
    analysis.add_argument("--max-side", type=int, default=512)
    analysis.add_argument("--decode", choices=("cuda", "cpu"), default="cuda",
                          help="Декодер FFmpeg; модели всегда используют GPU")
    reporting = commands.add_parser("report", help="Пересоздать отчёт из сохранённых результатов")
    reporting.add_argument("--run", type=Path, default=RUNS / "smoke")
    rendering = commands.add_parser("render", help="Собрать MP4 по монтажному плану без повторного анализа")
    rendering.add_argument("--plan", type=Path, required=True)
    rendering.add_argument("--output", type=Path, required=True)
    rendering.add_argument("--check", action="store_true", help="Только проверить план и исходники")
    editing = commands.add_parser("edit", help="Локальный редактор монтажа")
    editing.add_argument("--plan", type=Path, required=True)
    editing.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            from .pipeline import doctor
            result = doctor(args.output.resolve(), require_models=not args.skip_models)
            print(f"Doctor OK: {result['gpu']['gpu']}")
        elif args.command == "scan":
            from .media import scan
            source = args.source.resolve()
            output = args.output.resolve()
            if output == source or source in output.parents:
                raise AnalysisError("Каталог результатов должен находиться вне папки исходников")
            result = scan(args.source)
            atomic_json(output, result)
            print(f"Media: {len(result['media'])}; errors: {len(result['errors'])}; output: {args.output}")
            return 1 if result["errors"] else 0
        elif args.command == "analyze":
            from .pipeline import analyze
            if args.max_side < 64 or args.max_side > 2048:
                raise AnalysisError("max-side должен находиться в диапазоне 64..2048")
            output = args.output or RUNS / args.profile
            analyze(args.source, output, args.profile, window_s=args.window_s, fps=args.fps,
                    max_side=args.max_side, decode=args.decode)
            print(f"Analysis complete: {output / 'analysis.md'}")
        elif args.command == "render":
            from .render import load_plan, render
            if args.check:
                plan = load_plan(args.plan)
                print(f"Plan OK: {len(plan['shots'])} shots, {plan['duration_s']:.2f}s")
            else:
                render(args.plan, args.output)
                print(f"Render complete: {args.output / 'film.mp4'}")
        elif args.command == "edit":
            from .editor import serve
            if not 0 <= args.port <= 65535:
                raise AnalysisError("Порт должен быть в диапазоне 0..65535")
            serve(args.plan, args.port)
        else:
            from .report import report
            print(report(args.run.resolve()))
        return 0
    except KeyboardInterrupt:
        print("Прервано. Готовые операции сохранены в кеше.", file=sys.stderr)
        return 130
    except (AnalysisError, OSError, ValueError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
