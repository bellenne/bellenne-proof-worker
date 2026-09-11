from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from pydantic import ValidationError

from app.core.errors import WorkerError
from app.core.lifecycle import configure_imaging
from app.models.preset import Preset
from app.storage.workspace import atomic_json


def process(source: Path, output: Path, preset: Preset, data_path: Path, diagnostic: bool = False):
    configure_imaging(data_path)
    from app.imaging.analysis.analyzer import CropAnalyzer
    from app.imaging.analysis.diagnostics import diagnostic_preview
    from app.imaging.loader import SourceValidator
    from app.imaging.preview import PreviewGenerator
    from app.imaging.renderer.proof_renderer import ProofRenderer

    started = perf_counter()
    step_started = perf_counter()
    metadata = SourceValidator().validate(source, preset)
    metrics = {"validation_duration_ms": (perf_counter() - step_started) * 1000}
    step_started = perf_counter()
    preview = PreviewGenerator().generate(source, preset)
    metrics["preview_duration_ms"] = (perf_counter() - step_started) * 1000
    step_started = perf_counter()
    analysis = CropAnalyzer().analyze(preview, metadata.width, metadata.height, preset)
    metrics["analysis_duration_ms"] = (perf_counter() - step_started) * 1000
    if diagnostic:
        diagnostic_preview(preview, analysis, data_path / "diagnostic_preview.jpg", preset.scoring)
    artifact = ProofRenderer().render(source, analysis.best_candidate.crop, preset, output, metadata)
    diagnostics = {**artifact.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json"),
                   **metrics,
                   "warnings": list(dict.fromkeys(metadata.warnings + analysis.warnings)),
                   "total_duration_ms": (perf_counter() - started) * 1000}
    atomic_json(data_path / "diagnostics.json", diagnostics)
    return diagnostics


def main():
    parser = argparse.ArgumentParser(description="Run RGB proof pipeline without Proof Core")
    parser.add_argument("command", nargs="?", choices=["process"], default="process")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preset", type=Path)
    parser.add_argument("--data-path", type=Path, default=Path("/data/dev"))
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    try:
        preset = Preset.model_validate_json(args.preset.read_text(encoding="utf-8")) if args.preset else Preset()
        result = process(args.source, args.output, preset, args.data_path, args.diagnostic)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except WorkerError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2))
        raise SystemExit(1) from None
    except (ValidationError, OSError, ValueError):
        print(json.dumps({"code": "INVALID_CONFIG", "message": "Cannot read CLI configuration or write diagnostics"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
