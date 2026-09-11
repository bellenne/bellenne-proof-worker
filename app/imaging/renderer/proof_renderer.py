from __future__ import annotations

import hashlib
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pyvips

from app.core.errors import WorkerError
from app.imaging.analysis.result import NormalizedCrop
from app.imaging.color.manager import ColorManager
from app.imaging.loader import open_source
from app.imaging.metadata import SourceMetadata
from app.imaging.renderer.thumbnail import create_thumbnail
from app.models.preset import Preset
from app.models.result import ProofArtifact


class ProofRenderer:
    """Render original source pixels, then atomically commit an RGB JPEG."""

    def render(self, path: Path, crop: NormalizedCrop, preset: Preset, output: Path, metadata: SourceMetadata) -> ProofArtifact:
        path, output = Path(path), Path(output)
        if path.resolve() == output.resolve():
            raise WorkerError("OUTPUT_WRITE_ERROR", "Output must not replace production source")
        temporary: Path | None = None
        started = perf_counter()
        try:
            if not metadata.matches_file(path):
                raise WorkerError("INVALID_IMAGE", "Source changed after validation")
            source = open_source(path)
            if (source.width, source.height) != (metadata.width, metadata.height):
                raise WorkerError("INVALID_IMAGE", "Source dimensions changed after validation")
            x, y, width, height = crop.to_pixels(source.width, source.height)
            if (width, height) != (preset.width_px, preset.height_px) or x < 0 or y < 0 or x + width > source.width or y + height > source.height:
                raise WorkerError("RENDER_ERROR", "Selected crop must match proof dimensions at one source pixel per output pixel", {"crop": [x, y, width, height], "required": [preset.width_px, preset.height_px]})
            # Crop FIRST. Only the final proof-sized region becomes a private
            # in-memory RGB image. It never comes from the analysis preview.
            main = ColorManager.rgb8(source.crop(x, y, width, height), preset.alpha_background).copy_memory()
            thumbnail, (thumb_x, thumb_y), rectangle = create_thumbnail(path, crop, preset)
            result = main.insert(thumbnail, thumb_x, thumb_y, expand=False)
            result = result.copy(xres=preset.output_dpi / 25.4, yres=preset.output_dpi / 25.4, interpretation="srgb")
            # Discard source EXIF rotation, embedded previews and stale size
            # tags. Keep only the exact RGB ICC bytes, with new output DPI.
            for field in result.get_fields():
                if field.startswith(("exif-", "xmp-", "iptc-")) or field in {"orientation", "icc-profile-data", "page-height", "n-pages"}:
                    result.remove(field)
            result.set_type(pyvips.GValue.gstr_type, "resolution-unit", "in")
            if metadata.icc_profile:
                result.set_type(pyvips.GValue.blob_type, "icc-profile-data", metadata.icc_profile)
            if not metadata.matches_file(path):
                raise WorkerError("INVALID_IMAGE", "Source changed during rendering")
            render_duration_ms = (perf_counter() - started) * 1000
            save_started = perf_counter()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.name}.{uuid4().hex}.part")
            # A sibling temporary file gives atomic rename on the output mount.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            result.jpegsave(str(temporary), Q=preset.jpeg_quality, subsample_mode="off", optimize_coding=True, interlace=False)
            # Windows _commit (os.fsync) requires a writable handle too.
            with temporary.open("r+b") as saved:
                digest = hashlib.file_digest(saved, "sha256").hexdigest()
                os.fsync(saved.fileno())
            size_bytes = temporary.stat().st_size
            os.replace(temporary, output)
            temporary = None
            if os.name == "posix":
                descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            diagnostics = {**metadata.to_processing_dict(), "output_width": width, "output_height": height, "output_dpi": preset.output_dpi, "output_color_space": "RGB", "output_icc_present": bool(metadata.icc_profile), "crop": crop.model_dump(), "crop_pixels": {"x": x, "y": y, "width": width, "height": height}, "thumbnail": {"x": thumb_x, "y": thumb_y, "width": thumbnail.width, "height": thumbnail.height, "crop_rectangle": list(rectangle)}, "render_duration_ms": render_duration_ms, "save_duration_ms": (perf_counter() - save_started) * 1000}
            return ProofArtifact(path=output, sha256=digest, size_bytes=size_bytes, metadata=diagnostics)
        except WorkerError:
            raise
        except OSError as error:
            raise WorkerError("OUTPUT_WRITE_ERROR", "Could not atomically save proof output", {"reason": str(error)[:500]}, retryable=True) from error
        except (pyvips.Error, ValueError) as error:
            raise WorkerError("RENDER_ERROR", "Could not render the RGB proof", {"reason": str(error)[:500]}) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
