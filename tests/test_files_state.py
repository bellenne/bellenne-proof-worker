import asyncio
import json
import os
import time

import pytest

from app.core.errors import WorkerError
from app.core.lifecycle import ProcessLock
from app.files.finder import FileFinder
from app.health import healthy
from app.logging.setup import redact
from app.models.preset import Preset, SearchConfig, mm_to_px
from app.storage.state import LocalState
from app.storage.workspace import Workspace, atomic_json
from app.worker.heartbeat import interruptible_wait


def order_tree(tmp_path):
    root = tmp_path / "source"
    order = root / "2026" / "Заказ 123"
    for revision in ("1", "2", "3"):
        (order / revision / "Исходник").mkdir(parents=True)
    return root, order


def test_physical_dimensions_and_preset():
    preset = Preset()
    assert (preset.width_px, preset.height_px) == (1701, 850)
    assert mm_to_px(150) == 425
    assert mm_to_px(50) == 142
    assert Preset(alpha_background="white").alpha_background == (255, 255, 255)
    with pytest.raises(ValueError):
        mm_to_px(float("nan"))
    with pytest.raises(ValueError):
        Preset(unknown_business_parameter=1)


def test_layout_is_found_in_its_only_revision_folder(tmp_path):
    root, order = order_tree(tmp_path)
    (order / "1" / "Исходник" / "Макет 1 первый.tif").touch()
    selected = order / "2" / "Исходник" / "Макет 3 нужный.tif"
    selected.touch()
    (order / "3" / "Исходник" / "Макет 8 другой.tif").touch()
    result = FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert result.path == selected
    assert result.diagnostics["selected_revision"] == 2


def test_layout_repeated_between_revision_folders_is_structure_error(tmp_path):
    root, order = order_tree(tmp_path)
    (order / "1" / "Исходник" / "Макет 3 первый.tif").touch()
    (order / "3" / "Исходник" / "Макет 3 повтор.tif").touch()
    with pytest.raises(WorkerError) as caught:
        FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert caught.value.code == "MULTIPLE_FILES_FOUND"
    assert caught.value.details["matching_revisions"] == [1, 3]


def test_layout_number_has_numeric_boundary(tmp_path):
    root, order = order_tree(tmp_path)
    (order / "3" / "Исходник" / "Макет 30 не третий.tif").touch()
    with pytest.raises(WorkerError) as caught:
        FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert caught.value.code == "FILE_NOT_FOUND"


def test_preview_is_ignored_and_tiff_is_selected(tmp_path):
    root, order = order_tree(tmp_path)
    preview = order / "3" / "Исходник" / "Макет 3 600х300.jpg"
    artwork = order / "3" / "Исходник" / "Макет 3 600х300.tiff"
    preview.touch()
    artwork.touch()
    assert FileFinder([root]).find("2026\\Заказ 123", 3, SearchConfig()).path == artwork


def test_preview_can_be_explicitly_allowed(tmp_path):
    root, order = order_tree(tmp_path)
    preview = order / "3" / "Исходник" / "Макет 3 600х300.jpg"
    preview.touch()
    config = SearchConfig(extension_priority=[".tif", ".tiff", ".jpg"])
    assert FileFinder([root]).find("2026/Заказ 123", 3, config).path == preview


def test_psd_only_layout_is_reported_as_unsupported(tmp_path):
    root, order = order_tree(tmp_path)
    psd = order / "2" / "Исходник" / "Макет 3 рабочий.psd"
    psd.touch()
    with pytest.raises(WorkerError) as caught:
        FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert caught.value.code == "UNSUPPORTED_FORMAT"
    assert caught.value.details["unsupported_matches"][0]["path"] == str(psd)


def test_equal_production_files_in_layout_directory_are_ambiguous(tmp_path):
    root, order = order_tree(tmp_path)
    (order / "3" / "Исходник" / "a").mkdir()
    (order / "3" / "Исходник" / "b").mkdir()
    (order / "3" / "Исходник" / "a" / "Макет 3 левый.tif").touch()
    (order / "3" / "Исходник" / "b" / "Макет 3 правый.tif").touch()
    with pytest.raises(WorkerError) as caught:
        FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert caught.value.code == "MULTIPLE_FILES_FOUND"


def test_only_immediate_numeric_directories_are_revisions(tmp_path):
    root, order = order_tree(tmp_path)
    (order / "не ревизия").mkdir()
    (order / "не ревизия" / "Макет 3.tif").touch()
    with pytest.raises(WorkerError) as caught:
        FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig())
    assert caught.value.code == "FILE_NOT_FOUND"


def test_layout_is_found_anywhere_inside_revision(tmp_path):
    root, order = order_tree(tmp_path)
    selected = order / "2" / "Произвольная папка" / "ещё глубже" / "Макет 3 production.tif"
    selected.parent.mkdir(parents=True)
    selected.touch()
    assert FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig()).path == selected


def test_search_does_not_depend_on_source_directory_spelling(tmp_path):
    root, order = order_tree(tmp_path)
    custom = order / "2" / "Исходиники"
    custom.mkdir()
    selected = custom / "Макет 3 нужный.tif"
    selected.touch()
    assert FileFinder([root]).find("2026/Заказ 123", 3, SearchConfig()).path == selected


@pytest.mark.parametrize("source_path", ["../Заказ", "/orders/123", "C:/orders/123", "//server/share", "\x00"])
def test_source_path_cannot_escape_mount(tmp_path, source_path):
    with pytest.raises(WorkerError) as caught:
        FileFinder([tmp_path]).find(source_path, 3, SearchConfig())
    assert caught.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("layout_number", [0, -1, True, "3"])
def test_layout_number_must_be_positive_integer(tmp_path, layout_number):
    with pytest.raises(WorkerError) as caught:
        FileFinder([tmp_path]).find("order", layout_number, SearchConfig())
    assert caught.value.code == "INVALID_CONFIG"


def test_missing_storage_and_missing_order_are_distinct(tmp_path):
    with pytest.raises(WorkerError) as caught:
        FileFinder([tmp_path / "missing"]).find("order", 3, SearchConfig())
    assert caught.value.code == "SOURCE_STORAGE_UNAVAILABLE" and caught.value.retryable
    with pytest.raises(WorkerError) as caught:
        FileFinder([tmp_path]).find("order", 3, SearchConfig())
    assert caught.value.code == "FILE_NOT_FOUND" and not caught.value.retryable
    assert caught.value.details["reason"] == "ORDER_DIRECTORY_NOT_FOUND"


def test_root_allowlist_and_marker(tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    with pytest.raises(WorkerError) as caught:
        FileFinder([mount]).find("order", 3, SearchConfig(roots=[str(tmp_path)]))
    assert caught.value.code == "INVALID_CONFIG"
    with pytest.raises(WorkerError) as caught:
        FileFinder([mount]).find("order", 3, SearchConfig(storage_marker=".online"))
    assert caught.value.code == "SOURCE_STORAGE_UNAVAILABLE"


def test_root_priority_resolves_duplicate_mounted_order(tmp_path):
    roots = [tmp_path / "main", tmp_path / "archive"]
    for root in roots:
        target = root / "order" / "3" / "Исходник"
        target.mkdir(parents=True)
        (target / "Макет 3.tif").touch()
    with pytest.raises(WorkerError):
        FileFinder(roots).find("order", 3, SearchConfig())
    selected = FileFinder(roots).find("order", 3, SearchConfig(root_priority=True))
    assert selected.path.is_relative_to(roots[0])


def test_state_reopen_preserves_result_and_attempt(tmp_path):
    path = tmp_path / "state.db"
    state = LocalState(path)
    state.claim({"id": "job", "attempt": 1})
    state.update("job", 1, artifact={"sha256": "abc"}, stage="UPLOADING")
    state.set_flag("claim_pending", True)
    state.close()
    state = LocalState(path)
    assert state.get("job", 1)["artifact"] == {"sha256": "abc"}
    assert state.get_flag("claim_pending") is True
    state.claim({"id": "job", "attempt": 2})
    state.detach_except("job", 2)
    assert state.get("job", 1)["stage"] == "DETACHED"
    assert state.get("job", 2)["artifact"] is None
    state.close()


def test_workspace_uses_untrusted_id_as_hash(tmp_path):
    workspace = Workspace(tmp_path / "data", tmp_path / "output", "../../escape", 1)
    assert workspace.path.is_relative_to(tmp_path / "data")
    assert workspace.result_path.is_relative_to(tmp_path / "output")


def test_process_lock_and_local_health(tmp_path):
    with ProcessLock(tmp_path / "worker.lock"):
        with pytest.raises(WorkerError):
            with ProcessLock(tmp_path / "worker.lock"):
                pass
    path = tmp_path / "health.json"
    atomic_json(path, {"pid": os.getpid(), "timestamp": time.time(), "fatal": False, "core_connected": False})
    assert healthy(path)
    atomic_json(path, {"pid": os.getpid(), "timestamp": time.time() - 500, "fatal": False})
    assert not healthy(path)


def test_redact_secrets_nested():
    value = {"authorization": "secret", "nested": [{"a": "aTOKENb", "password": "p"}]}
    text = json.dumps(redact(value, ("TOKEN",)))
    assert "TOKEN" not in text and "secret" not in text and '"p"' not in text


async def test_backoff_interrupts_on_shutdown():
    stop = asyncio.Event()
    task = asyncio.create_task(interruptible_wait(stop, 30))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 0.2)
