from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from brainlib.contracts import (
    Anchor,
    ContentVersion,
    Derivation,
    FileFingerprint,
    RetrievalMetadata,
    SourceRecord,
    SourceState,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic
from brainlib.inventory import InventoryItem


FIXED_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def make_retrieval_metadata(
    *, sha256: str = "a" * 64, byte_size: int = 7
) -> RetrievalMetadata:
    return RetrievalMetadata(
        "https://example.test/requested",
        "https://example.test/final",
        ("https://example.test/redirect",),
        FIXED_NOW,
        "text/html",
        byte_size,
        sha256,
        "approval-2026-09-04",
        FIXED_NOW,
        "one approved capture",
        "User approved one capture for this question.",
    )


def make_source_record(
    *,
    source_id: str | None = None,
    raw_path: PurePosixPath = PurePosixPath("notes/a.txt"),
    content_sha256: str = "a" * 64,
    retrieval: RetrievalMetadata | None = None,
) -> SourceRecord:
    checksum = content_sha256
    selected_source_id = (
        source_id_for_first_seen(raw_path, checksum) if source_id is None else source_id
    )
    derivation = Derivation(
        "drv_" + "b" * 64,
        checksum,
        "builtin.text",
        "1",
        "c" * 64,
        PurePosixPath(
            "sources/extracted",
            *raw_path.parts,
            checksum,
            "drv_" + "b" * 64 + ".md",
        ),
        "d" * 64,
        9,
        1,
        "ok",
        (Anchor("page", "1"),),
        FIXED_NOW,
        method="deterministic",
        method_metadata={
            "converter_id": "builtin.text",
            "converter_version": "builtin:builtin.text:1",
        },
    )
    version = ContentVersion(
        checksum,
        raw_path,
        7,
        FileFingerprint(raw_path, 7, 1),
        FIXED_NOW,
        () if retrieval is None else (retrieval,),
    )
    return SourceRecord(
        1,
        selected_source_id,
        raw_path,
        (),
        "text/plain",
        7,
        SourceState.OK,
        {checksum: version},
        checksum,
        {derivation.derivation_id: derivation},
        derivation.derivation_id,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
    )


def write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def make_integrity_inputs(repo_root: Path) -> tuple[SourceRecord, InventoryItem, str]:
    old_bytes, new_bytes = b"old", b"new"
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    live = write_bytes(repo_root / "sources/raw/notes/note.txt", new_bytes)
    live_stat = live.stat()
    record = replace(
        make_source_record(),
        source_id=source_id_for_first_seen(PurePosixPath("notes/note.txt"), old_sha),
        state=SourceState.INTEGRITY_ERROR,
        current_raw_path=PurePosixPath("notes/note.txt"),
        byte_size=len(old_bytes),
        versions={
            old_sha: ContentVersion(
                old_sha,
                PurePosixPath("notes/note.txt"),
                len(old_bytes),
                FileFingerprint(PurePosixPath("notes/note.txt"), len(old_bytes), 1),
                FIXED_NOW,
                (),
            )
        },
        active_content_sha256=old_sha,
        derivations={},
        active_derivation_id=None,
        diagnostics=(Diagnostic("raw_checksum_mismatch", "replacement detected"),),
    )
    item = InventoryItem(
        FileFingerprint(
            PurePosixPath("notes/note.txt"),
            len(new_bytes),
            live_stat.st_mtime_ns,
        ),
        "text/plain",
        ".txt",
        new_sha,
    )
    return record, item, old_sha


class StaticResolver:
    def __init__(self, value: bytes | None) -> None:
        self.value = value

    def read_exact(
        self, source_id: str, raw_path: PurePosixPath, sha256: str
    ) -> bytes | None:
        del source_id, raw_path, sha256
        return self.value
