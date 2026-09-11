from pathlib import Path

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
    path.write_bytes(b"rendered proof")
    return ProofArtifact(
        path=path,
        sha256=file_sha256(path),
        size_bytes=path.stat().st_size,
        metadata={"source_order_path": str(tmp_path / "source" / "orders" / "123")},
    )


def test_publishes_into_next_revision_with_business_filename(tmp_path):
    order = tmp_path / "source" / "orders" / "123"
    for revision in ("1", "2", "3"):
        (order / revision).mkdir(parents=True)
    workspace = Workspace(tmp_path / "data", tmp_path / "worker-output", "job-1", 1)
    artifact = OrderPublisher().publish(make_job(), workspace, make_artifact(tmp_path), order, [tmp_path / "source"])

    destination = order / "4" / "ЦП Макет 3 60х30.jpg"
    assert destination.read_bytes() == b"rendered proof"
    assert artifact.metadata["published_path"] == str(destination.resolve())
    assert artifact.metadata["published_revision"] == 4
    assert artifact.metadata["published_filename"] == destination.name
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
    assert [entry.name for entry in order.iterdir() if entry.name.isdecimal()] == ["3", "4"]


def test_filename_uses_preset_dimensions_in_centimetres():
    job = make_job().model_copy(update={"preset": Preset(proof_width_mm=455, proof_height_mm=205)})
    assert OrderPublisher.filename(job) == "ЦП Макет 3 45,5х20,5.jpg"
