from __future__ import annotations

from pathlib import Path

import pyvips

from app.core.errors import WorkerError
from app.imaging.color.manager import ColorManager
from app.imaging.headers import NativeHeader, probe_header
from app.imaging.metadata import SourceMetadata
from app.models.preset import Preset


def open_source(path: Path, header: NativeHeader | None = None) -> pyvips.Image:
    """Lazy, sequential decoding; only downstream requested regions materialize.

    Explicit loaders avoid interpreting filename brackets as libvips options.
    Disable operation caching so repeated sequential opens never reuse a
    consumed decoder. This does not disable libvips' bounded pixel tile cache.
    """
    header = header or probe_header(path)
    ColorManager.ensure_native_rgb(header.color_space)
    if header.icc_profile is not None:
        ColorManager.validate_profile(header.icc_profile)
    pyvips.cache_set_max(0)
    try:
        loader = {"png": pyvips.Image.pngload, "jpeg": pyvips.Image.jpegload, "tiff": pyvips.Image.tiffload}[header.format]
        image = loader(str(path), access="sequential", fail_on="warning")
        ColorManager.ensure_rgb(image, header.color_space)
        return image
    except pyvips.Error as error:
        raise WorkerError("INVALID_IMAGE", "libvips could not decode source metadata", {"reason": str(error)[:500]}) from error


class SourceValidator:
    def validate(self, path: Path, preset: Preset) -> SourceMetadata:
        path = Path(path)
        header = probe_header(path)
        image = open_source(path, header)
        if image.width < 1 or image.height < 1:
            raise WorkerError("INVALID_IMAGE", "Source dimensions must be positive")
        if image.width < preset.width_px or image.height < preset.height_px:
            raise WorkerError("SOURCE_TOO_SMALL", "Source is smaller than the required main crop", {"source_width": image.width, "source_height": image.height, "required_width": preset.width_px, "required_height": preset.height_px})
        profile = header.icc_profile if header.icc_profile is not None else ColorManager.profile(image)
        dpi = header.dpi_metadata
        try:
            stat = path.stat()
        except OSError as error:
            raise WorkerError("SOURCE_STORAGE_UNAVAILABLE", "Source storage became unavailable", retryable=True) from error
        return SourceMetadata(filename=path.name, extension=path.suffix.lower(), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, width=image.width, height=image.height, format=header.format, color_space="RGBA" if image.bands == 4 else "RGB", channels=image.bands, has_alpha=image.bands == 4, sample_format=image.format, icc_present=profile is not None, icc_name=ColorManager.profile_name(profile), icc_profile=profile, dpi_metadata=dpi, warnings=[] if profile else ["ICC_PROFILE_MISSING"])
