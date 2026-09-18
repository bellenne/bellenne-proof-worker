"""Synthetic native-libvips checks; no production artwork or whole-source arrays."""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import pyvips
from PIL import Image, ImageCms

from app.core.errors import WorkerError
from app.imaging.analysis.result import NormalizedCrop
from app.imaging.color.manager import ColorManager
from app.imaging.headers import probe_header
from app.imaging.loader import SourceValidator
from app.imaging.preview import PreviewGenerator
from app.imaging.renderer.overlay import outline
from app.imaging.renderer.proof_renderer import ProofRenderer
from app.imaging.renderer.thumbnail import crop_rectangle, thumbnail_dimensions, thumbnail_position
from app.models.preset import Preset, mm_to_px


@pytest.fixture
def preset() -> Preset:
    # At 25.4 DPI millimetres equal pixels, making source-pixel assertions clear.
    return Preset(proof_width_mm=128, proof_height_mm=64, output_dpi=25.4,
                  thumbnail_max_side_mm=20, thumbnail_left_offset_mm=10,
                  analysis_preview_max_side_px=64, jpeg_quality=100)


def rgb_profile(alternate: bool = False) -> bytes:
    profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    if alternate:
        # A valid synthetic RGB profile with a different red primary. Its
        # samples MUST be preserved even though it is no longer the sRGB gamut.
        count = struct.unpack_from(">I", profile, 128)[0]
        for index in range(count):
            signature, offset, _ = struct.unpack_from(">4sII", profile, 132 + index * 12)
            if signature == b"rXYZ":
                struct.pack_into(">i", profile, offset + 8, round(0.55 * 65536))
        profile[84:100] = bytes(16)  # Unset the old profile-ID checksum.
    return bytes(profile)


def make_source(path: Path, mode: str = "RGB", size=(256, 128), color=None, **options) -> Path:
    if color is None:
        color = {"RGB": (232, 30, 67), "RGBA": (232, 30, 67, 128),
                 "CMYK": (10, 30, 60, 15), "LAB": (128, 100, 100), "L": 180, "P": 1}[mode]
    Image.new(mode, size, color).save(path, **options)
    return path


def pixels(image: pyvips.Image) -> np.ndarray:
    return np.frombuffer(image.write_to_memory(), dtype=np.uint8).reshape(image.height, image.width, image.bands)


@pytest.mark.parametrize("mm,dpi,expected", [(600, 72, 1701), (300, 72, 850), (150, 72, 425), (50, 72, 142), (0, 72, 0)])
def test_mm_to_px(mm, dpi, expected):
    assert mm_to_px(mm, dpi) == expected


@pytest.mark.parametrize("mm,dpi", [(-1, 72), (10, 0), (10, -1), (float("nan"), 72), (10, float("inf"))])
def test_mm_to_px_rejects_invalid(mm, dpi):
    with pytest.raises(ValueError):
        mm_to_px(mm, dpi)


def test_default_proof_dimensions():
    assert (Preset().width_px, Preset().height_px) == (1701, 850)


@pytest.mark.parametrize("width,height,expected", [(400, 200, (20, 10)), (200, 400, (10, 20)), (300, 300, (20, 20)), (5, 10, (5, 10))])
def test_thumbnail_dimensions_and_aspect(width, height, expected):
    actual = thumbnail_dimensions(width, height, 20)
    assert actual == expected
    assert max(actual) <= 20
    assert abs(actual[0] / actual[1] - width / height) < 0.001


@pytest.mark.parametrize("alignment,y", [("top", 0), ("center", 27), ("bottom", 54)])
def test_thumbnail_position(preset, alignment, y):
    configured = preset.model_copy(update={"thumbnail_vertical_alignment": alignment})
    assert thumbnail_position(20, 10, configured) == (10, y)


def test_thumbnail_cannot_extend_canvas(preset):
    with pytest.raises(WorkerError, match="outside") as error:
        thumbnail_position(200, 10, preset)
    assert error.value.code == "RENDER_ERROR"


def test_crop_rectangle_maps_normalized_edges():
    crop = NormalizedCrop(x=0.25, y=0.2, width=0.5, height=0.4)
    assert crop_rectangle(crop, 200, 100) == (50, 20, 100, 40)
    assert crop_rectangle(NormalizedCrop(x=0.5, y=0.5, width=0.5, height=0.5), 5, 5) == (2, 2, 3, 3)


def test_outline_black_no_fill_and_no_canvas_growth():
    original = pyvips.Image.black(20, 20, bands=3).new_from_image([200, 100, 50]).cast("uchar")
    result = outline(original, (3, 4, 12, 10), 2)
    array = pixels(result)
    assert array.shape == (20, 20, 3)
    assert np.all(array[4:6, 3:15] == 0)
    assert np.all(array[12:14, 3:15] == 0)
    assert np.all(array[8, 8] == [200, 100, 50])
    assert np.all(pixels(original) == [200, 100, 50])


@pytest.mark.parametrize("extension,mode", [(".tif", "RGB"), (".png", "RGB"), (".jpg", "RGB"), (".png", "RGBA"), (".tif", "RGBA")])
def test_rgb_rgba_detection_and_reduced_preview(tmp_path, preset, extension, mode):
    source = make_source(tmp_path / ("source" + extension), mode)
    metadata = SourceValidator().validate(source, preset)
    assert metadata.color_space == mode
    assert metadata.channels == len(mode)
    assert metadata.has_alpha == (mode == "RGBA")
    assert metadata.warnings == ["ICC_PROFILE_MISSING"]
    assert metadata.matches_file(source)
    preview = PreviewGenerator().generate(source, preset)
    assert preview.shape == (32, 64, 3)
    assert preview.dtype == np.uint8
    # This also rejects an accidental RGB/BGR swap.
    assert preview[16, 32, 0] > preview[16, 32, 2]
    assert "icc_profile" not in metadata.model_dump()


@pytest.mark.parametrize("extension,mode", [(".tif", "CMYK"), (".jpg", "CMYK"), (".tif", "LAB"), (".png", "L"), (".jpg", "L"), (".tif", "L"), (".png", "P"), (".tif", "P")])
def test_unsupported_color_never_converted(tmp_path, preset, extension, mode):
    source = make_source(tmp_path / ("unsupported" + extension), mode)
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "UNSUPPORTED_COLOR_SPACE"
    assert error.value.details["detected_color_space"]
    assert not error.value.retryable


@pytest.mark.parametrize("suffix,payload,expected", [(".png", b"\x89PNG\r\n\x1a\n", "INVALID_IMAGE"), (".tif", b"II*\0", "INVALID_IMAGE"), (".jpg", b"\xff\xd8\xff", "INVALID_IMAGE"), (".bmp", b"BM", "UNSUPPORTED_FORMAT")])
def test_invalid_source_classified(tmp_path, preset, suffix, payload, expected):
    source = tmp_path / ("broken" + suffix)
    source.write_bytes(payload)
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == expected


def test_missing_source_classified(tmp_path, preset):
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(tmp_path / "missing.png", preset)
    assert error.value.code == "FILE_NOT_FOUND"


def test_source_too_small_reports_required_dimensions(tmp_path, preset):
    source = make_source(tmp_path / "small.png", size=(127, 64))
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "SOURCE_TOO_SMALL"
    assert error.value.details == {"source_width": 127, "source_height": 64, "required_width": 128, "required_height": 64}


@pytest.mark.parametrize("alternate", [False, True])
def test_icc_preserved_without_profile_transform(tmp_path, preset, alternate, monkeypatch):
    profile = rgb_profile(alternate)
    # LittleCMS independently confirms the synthetic alternative is valid RGB.
    assert ImageCms.ImageCmsProfile(io.BytesIO(profile)).profile.xcolor_space == "RGB "
    source = make_source(tmp_path / "profile.png", icc_profile=profile)
    metadata = SourceValidator().validate(source, preset)
    assert metadata.icc_present and metadata.icc_name
    assert metadata.icc_profile == profile
    assert metadata.warnings == []
    capture = {}

    def save(image, filename, **options):
        capture["pixels"] = pixels(image).copy()
        return pyvips.Operation.call("jpegsave", image, filename, **options)

    monkeypatch.setattr(pyvips.Image, "jpegsave", save, raising=False)
    crop = NormalizedCrop.from_pixels(80, 40, 128, 64, 256, 128)
    artifact = ProofRenderer().render(source, crop, preset, tmp_path / "result.jpg", metadata)
    assert np.all(capture["pixels"][:, 40:, :] == [232, 30, 67])
    with Image.open(artifact.path) as result:
        assert result.mode == "RGB"
        assert result.info["icc_profile"] == profile
    assert artifact.metadata["output_icc_present"] is True


@pytest.mark.parametrize("extension", [".png", ".jpg", ".tif"])
def test_non_rgb_embedded_icc_rejected(tmp_path, preset, extension):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    source = make_source(tmp_path / ("mismatch" + extension), icc_profile=profile)
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "UNSUPPORTED_COLOR_SPACE"


@pytest.mark.parametrize("extension", [".png", ".tif", ".jpg"])
def test_source_dpi_does_not_rescale_main_crop(tmp_path, preset, extension):
    source = make_source(tmp_path / ("dpi" + extension), dpi=(300, 300))
    metadata = SourceValidator().validate(source, preset)
    assert metadata.dpi_metadata["x"] == pytest.approx(300, abs=0.05)
    artifact = ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, tmp_path / "result.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.size == (128, 64)
        assert result.info["dpi"] == pytest.approx((25.4, 25.4), abs=0.01)
        assert "icc_profile" not in result.info


@pytest.mark.parametrize("background,alpha,expected", [((255, 255, 255), 0, (255, 255, 255)), ((12, 40, 80), 0, (12, 40, 80)), ((255, 255, 255), 255, (232, 30, 67)), ((20, 40, 80), 128, (126, 34, 73))])
def test_alpha_flattening_uses_preset_background(tmp_path, preset, background, alpha, expected):
    source = make_source(tmp_path / "alpha.png", "RGBA", color=(232, 30, 67, alpha))
    configured = preset.model_copy(update={"alpha_background": background})
    metadata = SourceValidator().validate(source, configured)
    preview = PreviewGenerator().generate(source, configured)
    assert preview[16, 32] == pytest.approx(expected, abs=1)
    artifact = ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), configured, tmp_path / "alpha.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.mode == "RGB"
        assert result.getpixel((100, 20)) == pytest.approx(expected, abs=2)


@pytest.mark.parametrize("extension,bands", [(".png", 3), (".tif", 3), (".png", 4), (".tif", 4)])
def test_unsigned_16bit_rgb_and_alpha(tmp_path, preset, extension, bands):
    values = [51400, 25700, 12850] + ([32768] if bands == 4 else [])
    image = pyvips.Image.black(256, 128, bands=bands).cast("ushort").new_from_image(values).copy(interpretation="rgb16")
    source = tmp_path / ("sixteen" + extension)
    if extension == ".png":
        image.pngsave(str(source), bitdepth=16)
    else:
        image.tiffsave(str(source), compression="deflate")
    metadata = SourceValidator().validate(source, preset)
    assert metadata.sample_format == "ushort"
    preview = PreviewGenerator().generate(source, preset)
    expected = (200, 100, 50) if bands == 3 else (227, 177, 152)
    assert preview[16, 32].astype(int) == pytest.approx(expected, abs=1)
    artifact = ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, tmp_path / "result.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.getpixel((100, 20)) == pytest.approx(expected, abs=2)


def test_renderer_extracts_exact_original_pixels_and_overlays_full_thumbnail(tmp_path, preset, monkeypatch):
    y, x = np.mgrid[:160, :300]
    original = np.stack([x % 256, y % 256, (x + y) % 256], axis=-1).astype(np.uint8)
    source = tmp_path / "original.png"
    Image.fromarray(original).save(source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = SourceValidator().validate(source, preset)
    captured = {}

    def save(image, filename, **options):
        captured["pixels"] = pixels(image).copy()
        return pyvips.Operation.call("jpegsave", image, filename, **options)

    monkeypatch.setattr(pyvips.Image, "jpegsave", save, raising=False)
    crop = NormalizedCrop.from_pixels(90, 70, 128, 64, 300, 160)
    artifact = ProofRenderer().render(source, crop, preset, tmp_path / "result.jpg", metadata)
    rendered = captured["pixels"]
    expected = original[70:134, 90:218].copy()
    thumb = artifact.metadata["thumbnail"]
    tx, ty, tw, th = (thumb[name] for name in ("x", "y", "width", "height"))
    outside = np.ones((64, 128), dtype=bool)
    outside[ty:ty + th, tx:tx + tw] = False
    np.testing.assert_array_equal(rendered[outside], expected[outside])
    assert max(tw, th) == 20
    assert tw / th == pytest.approx(300 / 160, abs=0.1)
    assert (tx, ty) == (10, (64 - th) // 2)
    assert np.all(rendered[ty, tx:tx + tw] == 0)
    rx, ry, rw, _rh = thumb["crop_rectangle"]
    assert np.all(rendered[ty + ry, tx + rx:tx + rx + rw] == 0)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256(artifact.path.read_bytes()).hexdigest() == artifact.sha256
    assert artifact.size_bytes == artifact.path.stat().st_size


@pytest.mark.parametrize("size", [(1800, 2500), (2500, 1000), (1800, 1800)])
def test_default_native_pipeline_vertical_horizontal_square(tmp_path, size):
    # Import lazily so unrelated validation checks do not depend on analyzer.
    from app.imaging.analysis.analyzer import CropAnalyzer
    source = tmp_path / "full.png"
    # Broad color detail survives preview reduction, unlike single-pixel noise.
    y, x = np.mgrid[:size[1], :size[0]]
    texture = np.stack([(x // 4) % 256, (y // 4) % 256, ((x + y) // 6) % 256], axis=-1).astype(np.uint8)
    Image.fromarray(texture).save(source)
    preset = Preset(analysis_preview_max_side_px=300)
    metadata = SourceValidator().validate(source, preset)
    preview = PreviewGenerator().generate(source, preset)
    analysis = CropAnalyzer().analyze(preview, metadata.width, metadata.height, preset)
    artifact = ProofRenderer().render(source, analysis.best_candidate.crop, preset, tmp_path / "result.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.size == (1701, 850)
        assert result.mode == "RGB"
        assert result.info["dpi"] == pytest.approx((72, 72), abs=0.01)
    assert analysis.candidate_count >= 1
    assert max(artifact.metadata["thumbnail"]["width"], artifact.metadata["thumbnail"]["height"]) <= 425


def test_render_failure_keeps_existing_result_and_removes_temporary(tmp_path, preset, monkeypatch):
    source = make_source(tmp_path / "source.png")
    metadata = SourceValidator().validate(source, preset)
    output = tmp_path / "output.jpg"
    output.write_bytes(b"previous completed artifact")

    def fail(image, filename, **options):
        Path(filename).write_bytes(b"incomplete JPEG")
        raise pyvips.Error("injected interrupted save")

    monkeypatch.setattr(pyvips.Image, "jpegsave", fail, raising=False)
    with pytest.raises(WorkerError) as error:
        ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, output, metadata)
    assert error.value.code == "RENDER_ERROR"
    assert output.read_bytes() == b"previous completed artifact"
    assert not list(tmp_path.glob("*.part"))


def test_source_change_after_validation_rejected(tmp_path, preset):
    source = make_source(tmp_path / "source.png")
    metadata = SourceValidator().validate(source, preset)
    make_source(source, color=(1, 2, 3), size=(300, 150))
    with pytest.raises(WorkerError) as error:
        ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, tmp_path / "out.jpg", metadata)
    assert error.value.code == "INVALID_IMAGE"


def test_output_cannot_replace_source(tmp_path, preset):
    source = make_source(tmp_path / "source.jpg")
    metadata = SourceValidator().validate(source, preset)
    with pytest.raises(WorkerError) as error:
        ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, source, metadata)
    assert error.value.code == "OUTPUT_WRITE_ERROR"


def test_multipage_tiff_is_ambiguous(tmp_path, preset):
    source = tmp_path / "pages.tif"
    Image.new("RGB", (256, 128)).save(source, save_all=True, append_images=[Image.new("RGB", (256, 128))])
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "UNSUPPORTED_FORMAT"


@pytest.mark.large
def test_large_bigtiff_streaming_pipeline(tmp_path):
    source = tmp_path / "large.tif"
    # 720 MB of RGB pixels, built lazily and stored compressed. This test only
    # materializes the reduced preview and the 1701x850 result in Python RAM.
    image = pyvips.Image.black(20000, 12000, bands=3).new_from_image([54, 140, 230]).cast("uchar").copy(interpretation="srgb")
    image.tiffsave(str(source), compression="deflate", tile=True, bigtiff=True)
    assert probe_header(source).color_space == "RGB"
    preset = Preset(analysis_preview_max_side_px=256)
    metadata = SourceValidator().validate(source, preset)
    preview = PreviewGenerator().generate(source, preset)
    assert max(preview.shape[:2]) == 256
    crop = NormalizedCrop.from_pixels(18000, 11000, preset.width_px, preset.height_px, 20000, 12000)
    artifact = ProofRenderer().render(source, crop, preset, tmp_path / "large-result.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.size == (preset.width_px, preset.height_px)
        assert result.getpixel((1500, 700)) == pytest.approx((54, 140, 230), abs=2)


def test_float_rgb_rejected_without_quantization():
    image = pyvips.Image.black(4, 4, bands=3).cast("float").copy(interpretation="srgb")
    with pytest.raises(WorkerError) as error:
        ColorManager.ensure_rgb(image)
    assert error.value.code == "UNSUPPORTED_FORMAT"


def test_missing_jpeg_dpi_does_not_report_decoder_default(tmp_path, preset):
    source = make_source(tmp_path / "no-dpi.jpg")
    assert SourceValidator().validate(source, preset).dpi_metadata is None


def test_jpeg_exif_dpi_and_orientation_are_metadata_only(tmp_path, preset):
    source = tmp_path / "exif.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Do not secretly rotate production pixels.
    exif[282], exif[283], exif[296] = 300, 300, 2
    make_source(source, exif=exif)
    metadata = SourceValidator().validate(source, preset)
    assert metadata.dpi_metadata == {"x": 300, "y": 300, "unit": "dpi"}
    assert (metadata.width, metadata.height) == (256, 128)
    artifact = ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, tmp_path / "result.jpg", metadata)
    with Image.open(artifact.path) as result:
        assert result.size == (128, 64)
        assert result.getexif().get(274, 1) == 1


def test_missing_source_during_render_classified_as_storage_failure(tmp_path, preset):
    source = make_source(tmp_path / "source.png")
    metadata = SourceValidator().validate(source, preset)
    source.unlink()
    with pytest.raises(WorkerError) as error:
        ProofRenderer().render(source, NormalizedCrop(x=0, y=0, width=0.5, height=0.5), preset, tmp_path / "out.jpg", metadata)
    assert error.value.code == "SOURCE_STORAGE_UNAVAILABLE"
    assert error.value.retryable


def test_invalid_embedded_icc_is_not_silently_dropped(tmp_path, preset):
    source = make_source(tmp_path / "bad-icc.png", icc_profile=b"invalid profile")
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "INVALID_IMAGE"


def test_compressed_png_icc_is_bounded(tmp_path, preset, monkeypatch):
    import app.imaging.headers as headers
    source = make_source(tmp_path / "large-icc.png", icc_profile=rgb_profile())
    monkeypatch.setattr(headers, "MAX_ICC_BYTES", 500)
    # Compressed bytes fit but uncompressed ICC profile exceeds the bound.
    assert len(zlib.compress(rgb_profile())) < 500
    with pytest.raises(WorkerError) as error:
        SourceValidator().validate(source, preset)
    assert error.value.code == "INVALID_IMAGE"


def test_filename_brackets_are_literal(tmp_path, preset):
    source = make_source(tmp_path / "source[shrink=8].png")
    metadata = SourceValidator().validate(source, preset)
    assert (metadata.width, metadata.height) == (256, 128)


def test_two_square_fragments_are_joined_and_both_marked_on_thumbnail(tmp_path, preset):
    pixels_array = np.zeros((128, 256, 3), dtype=np.uint8)
    pixels_array[:, :128] = (210, 30, 40)
    pixels_array[:, 128:] = (20, 190, 60)
    source = tmp_path / "two-fragments.png"
    Image.fromarray(pixels_array).save(source)
    metadata = SourceValidator().validate(source, preset)
    crops = [
        NormalizedCrop.from_pixels(32, 32, 64, 64, 256, 128),
        NormalizedCrop.from_pixels(160, 32, 64, 64, 256, 128),
    ]

    artifact = ProofRenderer().render_variant(
        source,
        crops,
        preset,
        tmp_path / "two.jpg",
        metadata,
        variant="two_fragments_30x30",
    )

    with Image.open(artifact.path) as image:
        assert image.size == (128, 64)
        assert image.getpixel((50, 50)) == pytest.approx((210, 30, 40), abs=3)
        assert image.getpixel((100, 50)) == pytest.approx((20, 190, 60), abs=3)
    assert len(artifact.metadata["thumbnail"]["crop_rectangles"]) == 2


@pytest.mark.parametrize(
    ("direction", "percent", "expected"),
    [("add", 25, (104, 79, 54)), ("subtract", 20, (97, 81, 65))],
)
def test_color_variant_duplicates_fragment_and_adjusts_right_saturation(
    tmp_path, preset, direction, percent, expected
):
    source = make_source(tmp_path / "color.png", color=(100, 80, 60))
    metadata = SourceValidator().validate(source, preset)
    crop = NormalizedCrop.from_pixels(80, 32, 64, 64, 256, 128)

    artifact = ProofRenderer().render_variant(
        source,
        [crop],
        preset,
        tmp_path / f"color-{direction}.jpg",
        metadata,
        variant="fragment_30x30_color",
        brightness_direction=direction,
        brightness_percent=percent,
    )

    with Image.open(artifact.path) as image:
        assert image.getpixel((50, 50)) == pytest.approx((100, 80, 60), abs=3)
        assert image.getpixel((100, 50)) == pytest.approx(expected, abs=3)
    assert artifact.metadata["brightness_direction"] == direction
    assert artifact.metadata["brightness_percent"] == percent


def test_90x30_joins_original_and_two_independent_saturation_corrections(
    tmp_path, preset
):
    source = make_source(tmp_path / "color-90x30.png", color=(100, 80, 60))
    square_preset = preset.model_copy(update={"proof_width_mm": preset.proof_width_mm / 2})
    render_preset = preset.model_copy(update={"proof_width_mm": preset.proof_width_mm * 1.5})
    metadata = SourceValidator().validate(source, square_preset)
    crop = NormalizedCrop.from_pixels(80, 32, 64, 64, 256, 128)
    fragments = [
        {
            "proof_variant": "fragment_30x30",
            "brightness_direction": None,
            "brightness_percent": None,
        },
        {
            "proof_variant": "fragment_30x30_color",
            "brightness_direction": "add",
            "brightness_percent": 25,
        },
        {
            "proof_variant": "fragment_30x30_color",
            "brightness_direction": "subtract",
            "brightness_percent": 20,
        },
    ]

    artifact = ProofRenderer().render_variant(
        source,
        [crop],
        render_preset,
        tmp_path / "proof-90x30.jpg",
        metadata,
        variant="fragment_90x30",
        fragments=fragments,
    )

    with Image.open(artifact.path) as image:
        assert image.size == (192, 64)
        assert image.getpixel((50, 50)) == pytest.approx((100, 80, 60), abs=3)
        assert image.getpixel((100, 50)) == pytest.approx((104, 79, 54), abs=3)
        assert image.getpixel((170, 50)) == pytest.approx((97, 81, 65), abs=3)
    assert artifact.metadata["proof_variant"] == "fragment_90x30"
    assert artifact.metadata["fragments"] == fragments
    assert len(artifact.metadata["thumbnail"]["crop_rectangles"]) == 1


def test_90x30_keeps_exact_canvas_when_pixel_rounding_differs(tmp_path, preset):
    source = make_source(tmp_path / "rounding.png")
    square_preset = preset.model_copy(update={"proof_width_mm": 63.5})
    render_preset = preset.model_copy(update={"proof_width_mm": 190.5})
    metadata = SourceValidator().validate(source, square_preset)
    crop = NormalizedCrop.from_pixels(80, 32, 64, 64, 256, 128)

    artifact = ProofRenderer().render_variant(
        source,
        [crop],
        render_preset,
        tmp_path / "rounded-90x30.jpg",
        metadata,
        variant="fragment_90x30",
        fragments=[
            {"proof_variant": "fragment_30x30"},
            {
                "proof_variant": "fragment_30x30_color",
                "brightness_direction": "add",
                "brightness_percent": 5,
            },
            {
                "proof_variant": "fragment_30x30_color",
                "brightness_direction": "subtract",
                "brightness_percent": 5,
            },
        ],
        fragment_width_px=square_preset.width_px,
    )

    with Image.open(artifact.path) as image:
        assert image.size == (render_preset.width_px, render_preset.height_px)


def test_single_30x30_fragment_uses_square_output(tmp_path, preset):
    source = make_source(tmp_path / "square-fragment.png")
    square_preset = preset.model_copy(update={"proof_width_mm": preset.proof_width_mm / 2})
    metadata = SourceValidator().validate(source, square_preset)
    crop = NormalizedCrop.from_pixels(80, 32, 64, 64, 256, 128)

    artifact = ProofRenderer().render_variant(
        source,
        [crop],
        square_preset,
        tmp_path / "square.jpg",
        metadata,
        variant="fragment_30x30",
    )

    with Image.open(artifact.path) as image:
        assert image.size == (64, 64)
    assert artifact.metadata["proof_variant"] == "fragment_30x30"


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [((300, 150), (1772, 886)), ((150, 300), (886, 1772))],
)
def test_thumbnail_is_full_layout_with_30cm_max_side_at_150_dpi(
    tmp_path, preset, source_size, expected_size
):
    source = make_source(tmp_path / "layout.png", size=source_size)
    metadata = SourceValidator().validate(source, preset)

    artifact = ProofRenderer().render_thumbnail(
        source,
        preset,
        tmp_path / "thumbnail.jpg",
        metadata,
    )

    with Image.open(artifact.path) as image:
        assert image.size == expected_size
        assert image.info["dpi"] == pytest.approx((150, 150), abs=0.1)
    assert artifact.metadata["proof_variant"] == "thumbnail"
    assert artifact.metadata["thumbnail_max_side_mm"] == 300
