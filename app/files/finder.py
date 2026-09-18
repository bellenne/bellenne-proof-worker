from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from app.core.errors import WorkerError
from app.files.models import SourceFile
from app.files.revisions import parse_revision_name
from app.models.preset import SearchConfig


class FileFinder:
    """Find the unique numbered order folder containing ``Макет N`` safely."""

    unsupported_artwork_extensions = {".psd", ".psb"}

    def __init__(self, allowed_roots: list[Path]):
        self.allowed_roots = [Path(root).resolve() for root in allowed_roots]

    def _roots(self, config: SearchConfig) -> list[Path]:
        roots = [Path(root).resolve() for root in config.roots] if config.roots else self.allowed_roots
        if not roots or any(
            not any(root == allowed or root.is_relative_to(allowed) for allowed in self.allowed_roots)
            for root in roots
        ):
            raise WorkerError("INVALID_CONFIG", "Search roots must be inside configured source mounts")
        return list(dict.fromkeys(roots))

    @staticmethod
    def _relative_parts(source_path: str) -> tuple[str, ...]:
        raw = source_path.strip().replace("\\", "/")
        if (
            not raw
            or raw.startswith("/")
            or raw.startswith("//")
            or re.match(r"^[A-Za-z]:", raw)
            or "\x00" in raw
        ):
            raise WorkerError("INVALID_CONFIG", "Source path must be relative to a configured source root")
        path = PurePosixPath(raw)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise WorkerError("INVALID_CONFIG", "Source path contains an unsafe segment")
        return path.parts

    @staticmethod
    def _layout_pattern(layout_number: int) -> re.Pattern[str]:
        # The numeric boundary prevents ``Макет 3`` from matching ``Макет 30``.
        return re.compile(rf"^макет\s+0*{layout_number}(?=$|[\s_.()\-–—])", re.IGNORECASE)

    @staticmethod
    def _revision(name: str) -> int | None:
        return parse_revision_name(name)

    def find(self, source_path: str, layout_number: int, config: SearchConfig) -> SourceFile:
        if (
            isinstance(layout_number, bool)
            or not isinstance(layout_number, int)
            or not 1 <= layout_number <= 999_999
        ):
            raise WorkerError("INVALID_CONFIG", "Layout number must be a positive integer")
        parts = self._relative_parts(source_path)
        roots = self._roots(config)
        diagnostics = {
            "source_path": source_path,
            "layout_number": layout_number,
            "roots_checked": [str(root) for root in roots],
            "order_directories": [],
            "revision_directories": [],
            "search_directories": [],
            "matches": [],
            "unsupported_matches": [],
            "rules_applied": config.model_dump(),
        }
        order_directories: list[tuple[int, Path]] = []
        try:
            for root_index, root in enumerate(roots):
                if not root.is_dir():
                    raise OSError("Source mount missing")
                if config.storage_marker:
                    marker = (root / config.storage_marker).resolve()
                    if not marker.is_relative_to(root) or not marker.is_file():
                        raise OSError("Storage marker missing")
                order = root.joinpath(*parts)
                try:
                    resolved = order.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if not resolved.is_relative_to(root) or not resolved.is_dir():
                    continue
                order_directories.append((root_index, resolved))
                diagnostics["order_directories"].append(str(resolved))
        except PermissionError:
            raise WorkerError("FILE_ACCESS_DENIED", "Source storage cannot be read", diagnostics) from None
        except OSError:
            raise WorkerError(
                "SOURCE_STORAGE_UNAVAILABLE",
                "Source storage is unavailable while resolving the order folder",
                diagnostics,
            ) from None

        if not order_directories:
            diagnostics["reason"] = "ORDER_DIRECTORY_NOT_FOUND"
            raise WorkerError("FILE_NOT_FOUND", "Order directory was not found", diagnostics)

        pattern = self._layout_pattern(layout_number)
        candidates: list[tuple[Path, int, int, int, Path]] = []
        unsupported: list[tuple[Path, int, int]] = []
        try:
            for root_index, order in order_directories:
                revisions: list[tuple[int, Path]] = []
                for entry in order.iterdir():
                    revision = self._revision(entry.name)
                    if revision is not None and entry.is_dir() and not entry.is_symlink():
                        revisions.append((revision, entry))
                        diagnostics["revision_directories"].append(
                            {"path": str(entry), "revision": revision}
                        )
                for revision, revision_directory in revisions:
                    diagnostics["search_directories"].append(
                        {"path": str(revision_directory), "revision": revision}
                    )

                    def onerror(error):
                        raise error

                    for current, dirs, filenames in os.walk(
                        revision_directory, followlinks=False, onerror=onerror
                    ):
                        dirs[:] = (
                            sorted(
                                name for name in dirs
                                if not (Path(current) / name).is_symlink()
                            )
                            if config.recursive
                            else []
                        )
                        for filename in filenames:
                            file = Path(current) / filename
                            if not pattern.match(file.stem):
                                continue
                            resolved = file.resolve(strict=True)
                            if not resolved.is_relative_to(revision_directory) or not resolved.is_file():
                                continue
                            extension = file.suffix.casefold()
                            if extension in config.extension_priority:
                                candidates.append(
                                    (resolved, revision, config.extension_priority.index(extension), root_index, order)
                                )
                            elif extension in self.unsupported_artwork_extensions:
                                unsupported.append((resolved, revision, root_index))
        except PermissionError:
            raise WorkerError("FILE_ACCESS_DENIED", "Order directory cannot be read", diagnostics) from None
        except OSError:
            raise WorkerError(
                "SOURCE_STORAGE_UNAVAILABLE",
                "Source storage changed or became unavailable during layout search",
                diagnostics,
            ) from None

        diagnostics["matches"] = [
            {"path": str(path), "revision": revision, "extension_priority": extension_priority}
            for path, revision, extension_priority, _, _ in sorted(candidates, key=lambda item: str(item[0]))
        ][:100]
        diagnostics["unsupported_matches"] = [
            {"path": str(path), "revision": revision}
            for path, revision, _ in sorted(unsupported, key=lambda item: str(item[0]))
        ][:100]

        matching_revisions = {
            candidate[1] for candidate in candidates
        } | {
            item[1] for item in unsupported
        }
        if not matching_revisions:
            diagnostics["reason"] = "LAYOUT_NOT_FOUND"
            raise WorkerError(
                "FILE_NOT_FOUND", f"No file beginning with 'Макет {layout_number}' was found", diagnostics
            )
        if len(matching_revisions) != 1:
            diagnostics["reason"] = "LAYOUT_FOUND_IN_MULTIPLE_REVISION_DIRECTORIES"
            diagnostics["matching_revisions"] = sorted(matching_revisions)
            raise WorkerError(
                "MULTIPLE_FILES_FOUND",
                "The requested layout exists in more than one numbered order directory",
                diagnostics,
            )
        selected_revision = matching_revisions.pop()
        diagnostics["selected_revision"] = selected_revision
        selected = [candidate for candidate in candidates if candidate[1] == selected_revision]
        if not selected:
            diagnostics["reason"] = "LAYOUT_FORMAT_UNSUPPORTED"
            raise WorkerError(
                "UNSUPPORTED_FORMAT",
                "The requested layout exists only in PSD/PSB, which this Worker cannot process",
                diagnostics,
            )

        best_extension = min(candidate[2] for candidate in selected)
        winners = [candidate for candidate in selected if candidate[2] == best_extension]
        if config.root_priority:
            best_root = min(candidate[3] for candidate in winners)
            winners = [candidate for candidate in winners if candidate[3] == best_root]
        if len(winners) != 1:
            diagnostics["reason"] = "AMBIGUOUS_LATEST_LAYOUT"
            raise WorkerError(
                "MULTIPLE_FILES_FOUND",
                "More than one production file matches the layout in its numbered directory",
                diagnostics,
            )
        diagnostics["selected_path"] = str(winners[0][0])
        diagnostics["selected_order_directory"] = str(winners[0][4])
        return SourceFile(
            path=winners[0][0], order_path=winners[0][4], revision=selected_revision,
            diagnostics=diagnostics,
        )
