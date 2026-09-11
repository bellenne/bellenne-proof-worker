from __future__ import annotations

import struct

import pyvips

from app.core.errors import WorkerError


class ColorManager:
    @staticmethod
    def ensure_native_rgb(native_color: str | None) -> None:
        if native_color is not None and native_color not in {"RGB", "RGBA"}:
            raise WorkerError("UNSUPPORTED_COLOR_SPACE", "Only RGB and RGBA sources are supported", {"detected_color_space": native_color})

    @staticmethod
    def ensure_rgb(image: pyvips.Image, native_color: str | None = None) -> str:
        ColorManager.ensure_native_rgb(native_color)
        if image.interpretation not in {"srgb", "rgb", "rgb16"} or image.bands not in {3, 4}:
            raise WorkerError("UNSUPPORTED_COLOR_SPACE", "Only RGB and RGBA sources are supported", {"detected_color_space": image.interpretation, "channels": image.bands})
        if image.format not in {"uchar", "ushort"}:
            raise WorkerError("UNSUPPORTED_FORMAT", "Only unsigned 8-bit and 16-bit RGB samples are supported", {"sample_format": image.format})
        return "RGBA" if image.bands == 4 else "RGB"

    @staticmethod
    def rgb8(image: pyvips.Image, background: tuple[int, int, int]) -> pyvips.Image:
        """Composite alpha in native RGB values; quantize 16-bit JPEG samples.

        `srgb` is libvips' unsigned RGB storage interpretation here. Copying
        this tag changes no samples and assigns no embedded color profile.
        No colourspace(), icc_import(), or icc_transform() is performed.
        """
        ColorManager.ensure_rgb(image)
        if image.bands == 4:
            scale = 257 if image.format == "ushort" else 1
            image = image.flatten(background=[value * scale for value in background], max_alpha=65535 if scale == 257 else 255)
        if image.format == "ushort":
            image = image.cast("uchar", shift=True)
        return image.copy(interpretation="srgb")

    @staticmethod
    def profile(image: pyvips.Image) -> bytes | None:
        if not image.get_typeof("icc-profile-data"):
            return None
        profile = bytes(image.get("icc-profile-data"))
        return ColorManager.validate_profile(profile)

    @staticmethod
    def validate_profile(profile: bytes) -> bytes:
        if (len(profile) < 132 or profile[36:40] != b"acsp"
                or struct.unpack_from(">I", profile)[0] != len(profile)):
            raise WorkerError("INVALID_IMAGE", "Embedded ICC profile has an invalid header")
        if profile[16:20] != b"RGB ":
            raise WorkerError("UNSUPPORTED_COLOR_SPACE", "Embedded ICC profile is not RGB", {"detected_color_space": profile[16:20].decode("ascii", errors="replace")})
        return profile

    @staticmethod
    def profile_name(profile: bytes | None) -> str | None:
        """Read ICC v2 desc / v4 mluc tags without a color-management engine."""
        if profile is None or len(profile) < 132:
            return None
        try:
            count = struct.unpack_from(">I", profile, 128)[0]
            if count > (len(profile) - 132) // 12:
                return None
            for index in range(count):
                signature, offset, size = struct.unpack_from(">4sII", profile, 132 + index * 12)
                if signature != b"desc" or offset + size > len(profile):
                    continue
                tag = profile[offset:offset + size]
                if tag[:4] == b"desc" and len(tag) >= 12:
                    length = struct.unpack_from(">I", tag, 8)[0]
                    return tag[12:12 + length].rstrip(b"\0").decode("utf-8", errors="replace")[:256] or None
                if tag[:4] == b"mluc" and len(tag) >= 28:
                    records, record_size = struct.unpack_from(">II", tag, 8)
                    if records and record_size >= 12:
                        length, start = struct.unpack_from(">II", tag, 20)
                        if start + length <= len(tag):
                            return tag[start:start + length].decode("utf-16-be", errors="replace")[:256] or None
        except (ValueError, struct.error):
            return None
        return None
