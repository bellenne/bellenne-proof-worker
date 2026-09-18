from pathlib import Path
import zipfile

from PIL import Image

import app.files.publisher as publisher_module
from app.files.publisher import OrderPublisher
from app.models.job import Job
from app.models.preset import Preset
from app.models.result import ProofArtifact
from app.storage.workspace import Workspace, file_sha256


def make_job() -> Job:
    return Job(
        job_id="job-1",
        source_path="orders/123",
        layout_number=3,
        attempt=1,
        preset=Preset(),
    )


def make_artifact(tmp_path: Path) -> ProofArtifact:
    path = tmp_path / "output" / "result.jpg"
    path.parent.mkdir()
    Image.new("RGB", (600, 300), (25, 80, 120)).save(
        path, format="JPEG", quality=95, dpi=(72, 72)
    )
    return ProofArtifact(
        path=path,
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
        metadata={"source_order_path": str(tmp_path / "source" / "orders" / "123")},
    )


def test_publishes_source_and_captioned_preview_into_next_revision(tmp_path, monkeypatch):
    order = tmp_path / "source" / "orders" / "123"
    for revision in ("1", "2", "3"):
        (order / revision).mkdir(parents=True)
    workspace = Workspace(tmp_path / "data", tmp_path / "worker-output", "job-1", 1)
    rendered = make_artifact(tmp_path)
    captions = []
    save_preview = publisher_module.save_captioned_preview

    def capture_caption(source, destination, caption, **options):
        captions.append(caption)
        return save_preview(source, destination, caption, **options)

    monkeypatch.setattr(publisher_module, "save_captioned_preview", capture_caption)
    artifact = OrderPublisher().publish(
        make_job(), workspace, rendered, order, [tmp_path / "source"]
    )

    source = order / "4" / "Исходник" / "ЦП Макет 3 60х30.jpg"
    preview = order / "4" / "Превью" / "ЦП Макет 3 60х30.jpg"
    assert source.read_bytes() == rendered.path.read_bytes()
    assert captions == ["ЦП Макет 3 60х30"]
    with Image.open(preview) as image:
        assert image.mode == "RGB"
        assert image.size == (600, 348)
        assert all(
            abs(actual - expected) <= 2
            for actual, expected in zip(image.getpixel((300, 150)), (25, 80, 120))
        )
        strip = image.crop((0, 300, 600, 348))
        assert min(channel[0] for channel in strip.getextrema()) < 40
        assert image.getpixel((5, 305)) == (255, 255, 255)
    assert artifact.metadata["published_path"] == str(preview.resolve())
    assert artifact.metadata["published_source_path"] == str(source.resolve())
    assert artifact.metadata["published_preview_path"] == str(preview.resolve())
    assert artifact.metadata["published_preview_sha256"] == file_sha256(preview)
    assert artifact.metadata["published_revision"] == 4
    assert artifact.metadata["published_filename"] == preview.name
    core_artifact = OrderPublisher.result_for_core(artifact, [tmp_path / "source"])
    assert core_artifact.path == preview.resolve()
    assert core_artifact.sha256 == file_sha256(preview)
    assert not list(order.glob(".proof-worker-*"))
    assert not (order / "4" / OrderPublisher.marker_name).exists()


def test_retry_reuses_published_revision(tmp_path):
    order = tmp_path / "source" / "orders" / "123"
    (order / "3").mkdir(parents=True)
    workspace = Workspace(tmp_path / "data", tmp_path / "worker-output", "job-1", 1)
    source = make_artifact(tmp_path)
    publisher = OrderPublisher()

    first = publisher.publish(make_job(), workspace, source, order, [tmp_path / "source"])
    second = publisher.publish(make_job(), workspace, source, order, [tmp_path / "source"])

    assert first.metadata["published_path"] == second.metadata["published_path"]
    assert first.metadata["published_preview_sha256"] == second.metadata["published_preview_sha256"]
    assert [entry.name for entry in order.iterdir() if entry.name.isdecimal()] == ["3", "4"]


def test_filename_uses_preset_dimensions_in_centimetres():
    job = make_job().model_copy(update={"preset": Preset(proof_width_mm=455, proof_height_mm=205)})
    assert OrderPublisher.filename(job) == "ЦП Макет 3 45,5х20,5.jpg"


def test_filename_follows_square_and_thumbnail_variants():
    job = make_job()
    assert OrderPublisher.filename(
        job.model_copy(update={"proof_variant": "fragment_30x30"})
    ) == "ЦП Макет 3 30х30.jpg"
    assert OrderPublisher.filename(
        job.model_copy(update={"proof_variant": "thumbnail"})
    ) == "ЦП Макет 3 Миниатюра.jpg"
    assert OrderPublisher.filename(
        job.model_copy(update={"proof_variant": "fragment_90x30"})
    ) == "ЦП Макет 3 90х30.jpg"


def test_batch_is_published_to_one_revision_and_archived(tmp_path):
    order = tmp_path / "source" / "orders" / "123"
    (order / "3").mkdir(parents=True)
    workspace = Workspace(tmp_path / "data", tmp_path / "worker-output", "job-1", 1)
    first = make_artifact(tmp_path)
    second_path = workspace.result_path_for(7)
    Image.new("RGB", (600, 300), (120, 40, 25)).save(second_path, format="JPEG")
    second_digest = file_sha256(second_path)
    batch = first.model_copy(update={"metadata": {
        **first.metadata,
        "result_kind": "render_batch",
        "batch_artifacts": [
            {
                "layout_number": 3,
                "path": str(first.path),
                "sha256": first.sha256,
                "size_bytes": first.size_bytes,
                "source_order_path": str(order),
            },
            {
                "layout_number": 7,
                "path": str(second_path),
                "sha256": second_digest,
                "size_bytes": second_path.stat().st_size,
                "source_order_path": str(order),
            },
        ],
    }})

    result = OrderPublisher().publish_many(
        make_job().model_copy(update={"layout_numbers": [3, 7]}),
        workspace,
        batch,
        [tmp_path / "source"],
    )

    assert result.metadata["result_kind"] == "preview_archive"
    assert result.metadata["published_revision"] == 4
    assert len(result.metadata["published_files"]) == 2
    assert (order / "4" / "Исходник" / "ЦП Макет 3 60х30.jpg").is_file()
    assert (order / "4" / "Исходник" / "ЦП Макет 7 60х30.jpg").is_file()
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist() == [
            "ЦП Макет 3 60х30.jpg",
            "ЦП Макет 7 60х30.jpg",
        ]
