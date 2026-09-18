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
from app.imaging.renderer.thumbnail import create_thumbnail_for_crops
from app.models.preset import Preset, mm_to_px
from app.models.result import ProofArtifact


class ProofRenderer:
    """Render original source pixels, then atomically commit an RGB JPEG."""

    def render(self, path: Path, crop: NormalizedCrop, preset: Preset, output: Path, metadata: SourceMetadata) -> ProofArtifact:
        return self.render_variant(
            path,
            [crop],
            preset,
            output,
            metadata,
            variant="fragment_60x30",
        )

    def render_variant(
        self,
        path: Path,
        crops: list[NormalizedCrop],
        preset: Preset,
        output: Path,
        metadata: SourceMetadata,
        *,
        variant: str,
        brightness_direction: str | None = None,
        brightness_percent: float | None = None,
        fragments: list[dict[str, object]] | None = None,
        fragment_width_px: int | None = None,
    ) -> ProofArtifact:
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
            if variant in {"fragment_60x30", "fragment_30x30"}:
                if len(crops) != 1:
                    raise WorkerError("RENDER_ERROR", "The single-fragment variant requires one crop")
                required_widths = [preset.width_px]
            elif variant == "two_fragments_30x30":
                if len(crops) != 2:
                    raise WorkerError("RENDER_ERROR", "The two-fragment variant requires two crops")
                required_widths = [fragment_width_px or round(preset.width_px / 2)] * 2
            elif variant == "fragment_30x30_color":
                if len(crops) != 1 or brightness_direction not in {"add", "subtract"} or brightness_percent is None:
                    raise WorkerError("RENDER_ERROR", "The color variant requires one crop and brightness settings")
                required_widths = [fragment_width_px or round(preset.width_px / 2)]
            elif variant == "fragment_90x30":
                if len(crops) != 1 or not isinstance(fragments, list) or len(fragments) != 3:
                    raise WorkerError(
                        "RENDER_ERROR",
                        "The 90x30 variant requires one crop and three fragment settings",
                    )
                required_widths = [fragment_width_px or round(preset.width_px / 3)]
            else:
                raise WorkerError("RENDER_ERROR", "Unsupported proof variant")

            crop_pixels = [crop.to_pixels(source.width, source.height) for crop in crops]
            parts: list[pyvips.Image] = []
            for pixels, required_width in zip(crop_pixels, required_widths, strict=True):
                x, y, width, height = pixels
                if (
                    (width, height) != (required_width, preset.height_px)
                    or x < 0
                    or y < 0
                    or x + width > source.width
                    or y + height > source.height
                ):
                    raise WorkerError(
                        "RENDER_ERROR",
                        "Selected crop must match the required fragment dimensions",
                        {"crop": [x, y, width, height], "required": [required_width, preset.height_px]},
                    )
                parts.append(
                    ColorManager.rgb8(
                        open_source(path).crop(x, y, width, height), preset.alpha_background
                    ).copy_memory()
                )
            if variant in {"fragment_60x30", "fragment_30x30"}:
                main = parts[0]
            elif variant == "two_fragments_30x30":
                main = parts[0].join(parts[1], "horizontal")
            elif variant == "fragment_30x30_color":
                adjusted = self._adjust_saturation(
                    parts[0], brightness_direction, brightness_percent
                )
                main = parts[0].join(adjusted, "horizontal")
            else:
                rendered_fragments: list[pyvips.Image] = []
                for fragment in fragments or []:
                    fragment_variant = fragment.get("proof_variant")
                    if fragment_variant == "fragment_30x30":
                        rendered_fragments.append(parts[0])
                    elif fragment_variant == "fragment_30x30_color":
                        rendered_fragments.append(
                            self._adjust_saturation(
                                parts[0],
                                fragment.get("brightness_direction"),
                                fragment.get("brightness_percent"),
                            )
                        )
                    else:
                        raise WorkerError(
                            "RENDER_ERROR", "The 90x30 variant contains an unsupported fragment"
                        )
                main = rendered_fragments[0]
                for fragment_image in rendered_fragments[1:]:
                    main = main.join(fragment_image, "horizontal")
            # Physical dimensions are rounded independently to whole pixels.
            # Normalize a possible one-pixel difference after joining panels.
            if main.width < preset.width_px:
                main = main.embed(0, 0, preset.width_px, preset.height_px, extend="copy")
            elif main.width > preset.width_px:
                main = main.crop(0, 0, preset.width_px, preset.height_px)
            thumbnail, (thumb_x, thumb_y), rectangles = create_thumbnail_for_crops(
                path, crops, preset
            )
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
            diagnostics = {
                **metadata.to_processing_dict(),
                "output_width": preset.width_px,
                "output_height": preset.height_px,
                "output_dpi": preset.output_dpi,
                "output_color_space": "RGB",
                "output_icc_present": bool(metadata.icc_profile),
                "proof_variant": variant,
                "crops": [crop.model_dump() for crop in crops],
                "crop_pixels": [
                    {"x": x, "y": y, "width": width, "height": height}
                    for x, y, width, height in crop_pixels
                ],
                "thumbnail": {
                    "x": thumb_x,
                    "y": thumb_y,
                    "width": thumbnail.width,
                    "height": thumbnail.height,
                    "crop_rectangle": list(rectangles[0]),
                    "crop_rectangles": [list(rectangle) for rectangle in rectangles],
                },
                "render_duration_ms": render_duration_ms,
                "save_duration_ms": (perf_counter() - save_started) * 1000,
            }
            if variant == "fragment_30x30_color":
                diagnostics["brightness_direction"] = brightness_direction
                diagnostics["brightness_percent"] = brightness_percent
            if variant == "fragment_90x30":
                diagnostics["fragments"] = fragments
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

    @staticmethod
    def _adjust_saturation(
        image: pyvips.Image,
        direction: object,
        percent: object,
    ) -> pyvips.Image:
        if (
            direction not in {"add", "subtract"}
            or isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not 0 < percent <= 100
        ):
            raise WorkerError("RENDER_ERROR", "Invalid saturation correction settings")
        factor = 1 + percent / 100
        if direction == "subtract":
            factor = 1 - percent / 100
        # The amoCRM field is named "brightness", but production uses that
        # word for saturation. Rec. 709 luminance keeps overall lightness while
        # the distance of each RGB channel from neutral grey is scaled.
        red, green, blue = 0.2126, 0.7152, 0.0722
        inverse = 1 - factor
        matrix = pyvips.Image.new_from_array([
            [factor + inverse * red, inverse * green, inverse * blue],
            [inverse * red, factor + inverse * green, inverse * blue],
            [inverse * red, inverse * green, factor + inverse * blue],
        ])
        return image.recomb(matrix).cast("uchar")

    def render_thumbnail(
        self,
        path: Path,
        preset: Preset,
        output: Path,
        metadata: SourceMetadata,
    ) -> ProofArtifact:
        """Render the entire layout with a 30 cm maximum side at 150 DPI."""
        path, output = Path(path), Path(output)
        if path.resolve() == output.resolve():
            raise WorkerError("OUTPUT_WRITE_ERROR", "Output must not replace production source")
        temporary: Path | None = None
        started = perf_counter()
        output_dpi = 150.0
        target_max_side = mm_to_px(300, output_dpi)
        try:
            if not metadata.matches_file(path):
                raise WorkerError("INVALID_IMAGE", "Source changed after validation")
            source = open_source(path)
            if (source.width, source.height) != (metadata.width, metadata.height):
                raise WorkerError("INVALID_IMAGE", "Source dimensions changed after validation")
            scale = target_max_side / max(source.width, source.height)
            result = ColorManager.rgb8(source, preset.alpha_background).resize(scale)
            if max(result.width, result.height) != target_max_side:
                raise WorkerError(
                    "RENDER_ERROR",
                    "Could not produce the required thumbnail dimensions",
                    {"required_max_side": target_max_side},
                )
            result = result.copy(
                xres=output_dpi / 25.4,
                yres=output_dpi / 25.4,
                interpretation="srgb",
            )
            for field in result.get_fields():
                if field.startswith(("exif-", "xmp-", "iptc-")) or field in {
                    "orientation",
                    "icc-profile-data",
                    "page-height",
                    "n-pages",
                }:
                    result.remove(field)
            result.set_type(pyvips.GValue.gstr_type, "resolution-unit", "in")
            if metadata.icc_profile:
                result.set_type(
                    pyvips.GValue.blob_type,
                    "icc-profile-data",
                    metadata.icc_profile,
                )
            if not metadata.matches_file(path):
                raise WorkerError("INVALID_IMAGE", "Source changed during rendering")
            render_duration_ms = (perf_counter() - started) * 1000
            save_started = perf_counter()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.name}.{uuid4().hex}.part")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            result.jpegsave(
                str(temporary),
                Q=preset.jpeg_quality,
                subsample_mode="off",
                optimize_coding=True,
                interlace=False,
            )
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
            return ProofArtifact(
                path=output,
                sha256=digest,
                size_bytes=size_bytes,
                metadata={
                    **metadata.to_processing_dict(),
                    "proof_variant": "thumbnail",
                    "output_width": result.width,
                    "output_height": result.height,
                    "output_dpi": output_dpi,
                    "output_color_space": "RGB",
                    "output_icc_present": bool(metadata.icc_profile),
                    "thumbnail_max_side_mm": 300,
                    "render_duration_ms": render_duration_ms,
                    "save_duration_ms": (perf_counter() - save_started) * 1000,
                },
            )
        except WorkerError:
            raise
        except OSError as error:
            raise WorkerError(
                "OUTPUT_WRITE_ERROR",
                "Could not atomically save proof thumbnail",
                {"reason": str(error)[:500]},
                retryable=True,
            ) from error
        except (pyvips.Error, ValueError) as error:
            raise WorkerError(
                "RENDER_ERROR",
                "Could not render the RGB proof thumbnail",
                {"reason": str(error)[:500]},
            ) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
