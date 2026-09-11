"""Small, bounded native-header probes, before a decoder expands a palette.

No pixel data is decoded here. In particular, TIFF PhotometricInterpretation
and PNG IHDR remain authoritative even when libvips would expose RGB bands.
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from app.core.errors import WorkerError


@dataclass(frozen=True)
class NativeHeader:
    format: str
    color_space: str | None = None
    dpi_metadata: dict[str, float | str] | None = None
    icc_profile: bytes | None = field(default=None, repr=False)


MAX_ICC_BYTES = 16 * 1024 * 1024


def _png_profile(payload: bytes) -> bytes:
    """Read iCCP before libpng can silently discard a mismatched profile."""
    name, separator, data = payload.partition(b"\0")
    if not separator or not 1 <= len(name) <= 79 or len(data) < 2 or data[0] != 0:
        raise ValueError("Invalid PNG ICC profile chunk")
    decoder = zlib.decompressobj()
    profile = decoder.decompress(data[1:], MAX_ICC_BYTES + 1)
    if len(profile) > MAX_ICC_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid or excessive PNG ICC profile")
    return profile


def _read_at(stream: BinaryIO, offset: int, size: int, file_size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > file_size:
        raise ValueError("Image header points outside the file")
    stream.seek(offset)
    value = stream.read(size)
    if len(value) != size:
        raise ValueError("Truncated image header")
    return value


def _tiff_header(stream: BinaryIO, magic: bytes, file_size: int, *, allow_next_ifd: bool = False) -> NativeHeader:
    order = "<" if magic[:2] == b"II" else ">"
    version = struct.unpack(order + "H", magic[2:4])[0]
    big = version == 43
    if big:
        if struct.unpack(order + "HH", magic[4:8]) != (8, 0):
            raise ValueError("Invalid BigTIFF header")
        offset = struct.unpack(order + "Q", _read_at(stream, 8, 8, file_size))[0]
    else:
        offset = struct.unpack(order + "I", magic[4:8])[0]
    count_width, entry_width, inline_width = (8, 20, 8) if big else (2, 12, 4)
    count_format = "Q" if big else "H"
    count = struct.unpack(order + count_format, _read_at(stream, offset, count_width, file_size))[0]
    if not 1 <= count <= 4096:
        raise ValueError("Invalid or excessive TIFF directory size")
    entries = _read_at(stream, offset + count_width, count * entry_width, file_size)
    next_offset = offset + count_width + count * entry_width
    next_ifd = struct.unpack(order + ("Q" if big else "I"), _read_at(stream, next_offset, inline_width, file_size))[0]
    if next_ifd and not allow_next_ifd:
        raise WorkerError("UNSUPPORTED_FORMAT", "Multi-page TIFF is ambiguous and is not supported")
    tags: dict[int, tuple[int | float, ...]] = {}
    profile = None
    sizes = {1: 1, 3: 2, 4: 4, 5: 8, 7: 1, 16: 8}
    for index in range(count):
        entry = entries[index * entry_width:(index + 1) * entry_width]
        tag, kind = struct.unpack(order + "HH", entry[:4])
        if tag not in {262, 277, 338, 282, 283, 296, 34675}:
            continue
        elements = struct.unpack(order + ("Q" if big else "I"), entry[4:12] if big else entry[4:8])[0]
        if tag == 34675 and (kind not in {1, 7} or profile is not None):
            raise ValueError("Invalid TIFF ICC profile tag")
        max_elements = MAX_ICC_BYTES if tag == 34675 else 16
        if kind not in sizes or not 1 <= elements <= max_elements:
            raise ValueError("Invalid TIFF color/resolution tag")
        value_field = entry[12:20] if big else entry[8:12]
        byte_count = elements * sizes[kind]
        if byte_count <= inline_width:
            raw = value_field[:byte_count]
        else:
            value_offset = struct.unpack(order + ("Q" if big else "I"), value_field)[0]
            raw = _read_at(stream, value_offset, byte_count, file_size)
        if tag == 34675:
            profile = raw
            continue
        if kind == 5:
            values = struct.unpack(order + "II" * elements, raw)
            if any(values[i] == 0 for i in range(1, len(values), 2)):
                raise ValueError("Invalid TIFF resolution denominator")
            tags[tag] = tuple(values[i] / values[i + 1] for i in range(0, len(values), 2))
        else:
            tags[tag] = struct.unpack(order + {1: "B", 3: "H", 4: "I", 7: "B", 16: "Q"}[kind] * elements, raw)
    photometric = tags.get(262, (-1,))[0]
    channels = tags.get(277, (1,))[0]
    spaces = {0: "Grayscale", 1: "Grayscale", 3: "Indexed", 5: "CMYK", 6: "YCbCr", 8: "Lab", 9: "Lab", 10: "Lab"}
    if photometric == 2:
        color = "RGB" if channels == 3 else "RGBA" if channels == 4 and tags.get(338, (0,))[0] in {1, 2} else f"RGB+{channels - 3} extra channels"
    else:
        color = spaces.get(photometric, f"TIFF photometric {photometric}")
    resolution = None
    unit = tags.get(296, (2,))[0]
    if 282 in tags and 283 in tags and unit in {2, 3}:
        factor = 1.0 if unit == 2 else 2.54
        resolution = {"x": tags[282][0] * factor, "y": tags[283][0] * factor, "unit": "dpi"}
    return NativeHeader("tiff", color, resolution, profile)


def _jpeg_header(stream: BinaryIO, file_size: int) -> NativeHeader:
    """Inspect bounded pre-scan markers; never infer DPI from decoder defaults."""
    offset = 2
    resolution = exif_resolution = None
    color = None
    profile_parts: dict[int, bytes] = {}
    profile_count = profile_bytes = 0
    for _ in range(4096):
        marker = _read_at(stream, offset, 2, file_size)
        if marker[0] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        offset += 2
        if marker[1] == 0xFF:  # Legal marker padding.
            offset -= 1
            continue
        if marker[1] in {0xDA, 0xD9}:
            break
        if marker[1] in {0x01, 0xD8, *range(0xD0, 0xD8)}:
            continue
        length = struct.unpack(">H", _read_at(stream, offset, 2, file_size))[0]
        if length < 2:
            raise ValueError("Invalid JPEG segment length")
        payload = _read_at(stream, offset + 2, length - 2, file_size)
        offset += length
        if marker[1] == 0xE0 and payload.startswith(b"JFIF\0") and len(payload) >= 12:
            unit, x, y = struct.unpack_from(">BHH", payload, 7)
            if unit in {1, 2} and x > 0 and y > 0:
                factor = 1 if unit == 1 else 2.54
                resolution = {"x": x * factor, "y": y * factor, "unit": "dpi"}
        elif marker[1] == 0xE1 and payload.startswith(b"Exif\0\0"):
            exif = payload[6:]
            if exif[:4] not in {b"II*\0", b"MM\0*"}:
                raise ValueError("Invalid JPEG EXIF header")
            exif_resolution = _tiff_header(io.BytesIO(exif), exif[:16], len(exif), allow_next_ifd=True).dpi_metadata
        elif marker[1] == 0xE2 and payload.startswith(b"ICC_PROFILE\0"):
            if len(payload) < 14:
                raise ValueError("Invalid JPEG ICC segment")
            sequence, count = payload[12:14]
            if not 1 <= sequence <= count or sequence in profile_parts or profile_count not in {0, count}:
                raise ValueError("Inconsistent JPEG ICC segments")
            profile_count = count
            profile_bytes += len(payload) - 14
            if profile_bytes > MAX_ICC_BYTES:
                raise ValueError("Excessive JPEG ICC profile")
            profile_parts[sequence] = payload[14:]
        elif marker[1] in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if len(payload) < 6:
                raise ValueError("Invalid JPEG frame header")
            bands = payload[5]
            color = {1: "Grayscale", 3: "RGB", 4: "CMYK"}.get(bands, f"JPEG {bands} channels")
    else:
        raise ValueError("Excessive JPEG header markers")
    profile = None
    if profile_parts:
        if len(profile_parts) != profile_count:
            raise ValueError("Incomplete JPEG ICC profile")
        profile = b"".join(profile_parts[index] for index in range(1, profile_count + 1))
    return NativeHeader("jpeg", color, exif_resolution or resolution, profile)


def probe_header(path: Path) -> NativeHeader:
    """Identify supported content by signature, not extension alone."""
    if path.suffix.lower() not in {".tif", ".tiff", ".png", ".jpg", ".jpeg"}:
        raise WorkerError("UNSUPPORTED_FORMAT", "Source extension is not supported", {"extension": path.suffix})
    try:
        with path.open("rb") as stream:
            file_size = path.stat().st_size
            magic = stream.read(16)
            if magic.startswith(b"\x89PNG\r\n\x1a\n"):
                header = _read_at(stream, 8, 25, file_size)
                if header[:8] != b"\x00\x00\x00\rIHDR":
                    raise ValueError("Invalid PNG IHDR")
                mode = {0: "Grayscale", 2: "RGB", 3: "Indexed", 4: "Grayscale+alpha", 6: "RGBA"}.get(header[17], f"PNG color type {header[17]}")
                resolution = None
                profile = None
                offset = 33
                # pHYs must precede IDAT. Skip chunk payloads without reading them.
                for _ in range(4096):
                    chunk = _read_at(stream, offset, 8, file_size)
                    length, kind = struct.unpack(">I4s", chunk)
                    if kind in {b"IDAT", b"IEND"}:
                        break
                    if kind == b"pHYs" and length == 9:
                        x, y, unit = struct.unpack(">IIB", _read_at(stream, offset + 8, 9, file_size))
                        if unit == 1:
                            resolution = {"x": x * 0.0254, "y": y * 0.0254, "unit": "dpi"}
                    if kind == b"iCCP":
                        if profile is not None or length > MAX_ICC_BYTES:
                            raise ValueError("Duplicate or excessive PNG ICC profile")
                        profile = _png_profile(_read_at(stream, offset + 8, length, file_size))
                    offset += 12 + length
                else:
                    raise ValueError("Excessive PNG header chunks")
                return NativeHeader("png", mode, resolution, profile)
            if magic[:4] in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
                return _tiff_header(stream, magic, file_size)
            if magic.startswith(b"\xff\xd8\xff"):
                return _jpeg_header(stream, file_size)
            raise WorkerError("UNSUPPORTED_FORMAT", "Content is not a supported TIFF, PNG or JPEG")
    except FileNotFoundError as error:
        raise WorkerError("FILE_NOT_FOUND", "Source file no longer exists") from error
    except PermissionError as error:
        raise WorkerError("FILE_ACCESS_DENIED", "Source file is not readable") from error
    except OSError as error:
        raise WorkerError("SOURCE_STORAGE_UNAVAILABLE", "Cannot read source storage", retryable=True) from error
    except (ValueError, struct.error, zlib.error) as error:
        raise WorkerError("INVALID_IMAGE", str(error)) from error
