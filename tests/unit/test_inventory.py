from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import socket
import struct
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

import brainlib.inventory as inventory_module
from brainlib.contracts import source_id_for_first_seen
from brainlib.inventory import (
    InventoryItem,
    MediaDetector,
    UrlDescriptor,
    inventory_raw_sources,
    parse_url_descriptor,
    source_id_for_url_descriptor,
)
from brainlib.layout import RepoPaths


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/inventory"
URL_DESCRIPTOR_MEDIA_TYPE = "application/x.second-brain-url-descriptor"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_inventory_excludes_reserved_versions_web_sentinels_and_temps(
    repo_root: Path,
) -> None:
    write_bytes(repo_root / "sources/raw/notes/a.txt", b"visible")
    write_bytes(repo_root / "sources/raw/_versions/src_x/a/old.txt", b"hidden")
    write_bytes(repo_root / "sources/raw/_web/example/page.html", b"hidden")
    write_bytes(repo_root / "sources/raw/.brain-tmp-123", b"hidden")
    write_bytes(repo_root / "sources/raw/notes/source.brain.lock", b"hidden")
    write_bytes(repo_root / "sources/raw/notes/.DS_Store", b"hidden")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert [item.fingerprint.path.as_posix() for item in report.items] == [
        "notes/a.txt"
    ]


def test_inventory_keeps_nested_versions_and_web_directories(repo_root: Path) -> None:
    write_bytes(repo_root / "sources/raw/archive/_versions/old.txt", b"old")
    write_bytes(repo_root / "sources/raw/archive/_web/page.html", b"<html></html>")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert [item.fingerprint.path.as_posix() for item in report.items] == [
        "archive/_versions/old.txt",
        "archive/_web/page.html",
    ]


def test_inventory_orders_normalized_relative_paths_and_does_not_hash(
    repo_root: Path,
) -> None:
    write_bytes(repo_root / "sources/raw/z-last.txt", b"z")
    write_bytes(repo_root / "sources/raw/a/second.txt", b"second")
    write_bytes(repo_root / "sources/raw/a/first.txt", b"first")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert [item.fingerprint.path.as_posix() for item in report.items] == [
        "a/first.txt",
        "a/second.txt",
        "z-last.txt",
    ]
    assert all(item.sha256 is None for item in report.items)


def test_inventory_fingerprint_tracks_observed_file_metadata(repo_root: Path) -> None:
    source = write_bytes(repo_root / "sources/raw/notes/a.txt", b"visible")
    observed = source.lstat()

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == (
        InventoryItem(
            fingerprint=report.items[0].fingerprint,
            media_type="text/plain",
            extension=".txt",
            sha256=None,
        ),
    )
    assert report.items[0].fingerprint.path == PurePosixPath("notes/a.txt")
    assert report.items[0].fingerprint.byte_size == len(b"visible")
    assert report.items[0].fingerprint.mtime_ns == observed.st_mtime_ns


def test_detector_uses_pdf_magic_not_extension() -> None:
    assert MediaDetector().detect(FIXTURES / "renamed-pdf.txt") == "application/pdf"


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    (
        ("renamed-png.bin", b"\x89PNG\r\n\x1a\nrest", "image/png"),
        ("renamed-jpeg.bin", b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        ("renamed-tiff.bin", b"II*\x00rest", "image/tiff"),
        ("renamed-webp.bin", b"RIFF\x08\x00\x00\x00WEBPrest", "image/webp"),
    ),
)
def test_detector_uses_image_magic(
    tmp_path: Path, name: str, content: bytes, expected: str
) -> None:
    path = write_bytes(tmp_path / name, content)

    assert MediaDetector().detect(path) == expected


@pytest.mark.parametrize(
    ("family_member", "expected"),
    (
        ("word/document.xml", DOCX_MEDIA_TYPE),
        ("ppt/presentation.xml", PPTX_MEDIA_TYPE),
        ("xl/workbook.xml", XLSX_MEDIA_TYPE),
    ),
)
def test_detector_uses_real_ooxml_package_members_before_extension(
    tmp_path: Path, family_member: str, expected: str
) -> None:
    path = write_bytes(
        tmp_path / "renamed.bin",
        build_ooxml_zip(family_member),
    )

    assert MediaDetector().detect(path) == expected


def test_detector_recognizes_renamed_docx_fixture() -> None:
    assert MediaDetector().detect(FIXTURES / "renamed-docx.bin") == DOCX_MEDIA_TYPE


def test_renamed_docx_fixture_is_a_reproducible_valid_zip() -> None:
    expected = build_ooxml_zip("word/document.xml")

    assert (FIXTURES / "renamed-docx.bin").read_bytes() == expected
    with zipfile.ZipFile(io.BytesIO(expected)) as package:
        assert package.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
        ]


def test_detector_finds_ooxml_members_after_large_stored_body(tmp_path: Path) -> None:
    path = write_bytes(
        tmp_path / "renamed.bin",
        build_ooxml_zip("word/document.xml", first_body=b"x" * 70_000),
    )

    assert MediaDetector().detect(path) == DOCX_MEDIA_TYPE


def test_detector_rejects_fabricated_ooxml_marker_substrings(tmp_path: Path) -> None:
    path = write_bytes(
        tmp_path / "fabricated.bin",
        b"PK\x03\x04[Content_Types].xml word/document.xml",
    )

    assert MediaDetector().detect(path) != DOCX_MEDIA_TYPE


def test_detector_falls_back_when_zip_member_names_exceed_metadata_budget(
    tmp_path: Path,
) -> None:
    path = write_bytes(
        tmp_path / "over-budget.bin",
        build_ooxml_zip(
            "word/document.xml",
            first_member_name="x" * 40_000,
        ),
    )

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_accepts_structurally_coherent_deflated_ooxml(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "deflated.bin", build_deflated_ooxml_zip())

    assert MediaDetector().detect(path) == DOCX_MEDIA_TYPE


def test_detector_rejects_force_zip64_local_records(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "force-zip64.bin", build_force_zip64_ooxml_zip())

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_zip64_eocd_and_locator(tmp_path: Path) -> None:
    package = build_ooxml_zip("word/document.xml")
    eocd_offset = package.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    zip64_eocd = b"PK\x06\x06" + struct.pack("<Q", 44) + b"\x00" * 44
    zip64_locator = b"PK\x06\x07" + b"\x00" * 16
    malformed = (
        package[:eocd_offset] + zip64_eocd + zip64_locator + package[eocd_offset:]
    )
    path = write_bytes(tmp_path / "zip64-structures.bin", malformed)

    assert MediaDetector().detect(path) == "application/octet-stream"


@pytest.mark.parametrize(
    ("field_offset", "field_format", "value"),
    (
        (6, "<H", 45),
        (20, "<L", 0xFFFFFFFF),
        (24, "<L", 0xFFFFFFFF),
    ),
)
def test_detector_rejects_central_zip64_indicators(
    tmp_path: Path, field_offset: int, field_format: str, value: int
) -> None:
    package = mutate_central_field(
        build_ooxml_zip("word/document.xml"),
        "word/document.xml",
        field_offset,
        field_format,
        value,
    )
    path = write_bytes(tmp_path / "central-zip64.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_zip64_extra_indicator(tmp_path: Path) -> None:
    package = build_ooxml_zip(
        "word/document.xml",
        family_extra=b"\x01\x00\x00\x00",
    )
    path = write_bytes(tmp_path / "zip64-extra.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_nonzero_central_disk_start(tmp_path: Path) -> None:
    package = mutate_central_field(
        build_ooxml_zip("word/document.xml"),
        "word/document.xml",
        34,
        "<H",
        1,
    )
    path = write_bytes(tmp_path / "disk-start.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


@pytest.mark.parametrize("local_offset", (0xFFFFFF00, 0xFFFFFFFF))
def test_detector_rejects_out_of_range_local_offsets(
    tmp_path: Path, local_offset: int
) -> None:
    package = mutate_central_field(
        build_ooxml_zip("word/document.xml"),
        "word/document.xml",
        42,
        "<L",
        local_offset,
    )
    path = write_bytes(tmp_path / "bad-offset.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_overlapping_local_offsets(tmp_path: Path) -> None:
    package = build_ooxml_zip("word/document.xml")
    rels_offset = central_local_offset(package, "_rels/.rels")
    package = mutate_central_field(
        package,
        "word/document.xml",
        42,
        "<L",
        rels_offset,
    )
    path = write_bytes(tmp_path / "overlap.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_local_and_central_name_mismatch(tmp_path: Path) -> None:
    package = bytearray(build_ooxml_zip("word/document.xml"))
    local_offset = central_local_offset(package, "word/document.xml")
    name_size = struct.unpack_from("<H", package, local_offset + 26)[0]
    replacement = b"word/document.x_m"
    assert len(replacement) == name_size
    package[local_offset + 30 : local_offset + 30 + name_size] = replacement
    path = write_bytes(tmp_path / "name-mismatch.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_truncated_local_extra_span(tmp_path: Path) -> None:
    package = bytearray(build_ooxml_zip("word/document.xml"))
    local_offset = central_local_offset(package, "word/document.xml")
    struct.pack_into("<H", package, local_offset + 28, 0xFFFF)
    path = write_bytes(tmp_path / "truncated-local-extra.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_malformed_central_extra_tlv(tmp_path: Path) -> None:
    package = bytearray(
        build_ooxml_zip(
            "word/document.xml",
            family_extra=b"\xfe\xca\x01\x00x",
        )
    )
    central_offset = central_record_offset(package, "word/document.xml")
    name_size = struct.unpack_from("<H", package, central_offset + 28)[0]
    extra_offset = central_offset + 46 + name_size
    struct.pack_into("<H", package, extra_offset + 2, 5)
    path = write_bytes(tmp_path / "malformed-central-extra.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_truncated_central_record(tmp_path: Path) -> None:
    package = bytearray(build_ooxml_zip("word/document.xml"))
    eocd_offset = package.rfind(b"PK\x05\x06")
    central_size = struct.unpack_from("<L", package, eocd_offset + 12)[0]
    del package[eocd_offset - 1]
    eocd_offset -= 1
    struct.pack_into("<L", package, eocd_offset + 12, central_size - 1)
    path = write_bytes(tmp_path / "truncated-central.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_central_record_count_mismatch(tmp_path: Path) -> None:
    package = bytearray(build_ooxml_zip("word/document.xml"))
    eocd_offset = package.rfind(b"PK\x05\x06")
    struct.pack_into("<H", package, eocd_offset + 8, 4)
    struct.pack_into("<H", package, eocd_offset + 10, 4)
    path = write_bytes(tmp_path / "count-mismatch.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_duplicate_required_markers(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        package = build_ooxml_zip(
            "word/document.xml",
            extra_members=(("[Content_Types].xml", b"<Types/>"),),
        )
    path = write_bytes(tmp_path / "duplicate-marker.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_multiple_ooxml_families(tmp_path: Path) -> None:
    package = build_ooxml_zip(
        "word/document.xml",
        extra_members=(("ppt/presentation.xml", b"<presentation/>"),),
    )
    path = write_bytes(tmp_path / "multiple-families.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


def test_detector_rejects_encrypted_central_entry(tmp_path: Path) -> None:
    package = mutate_central_field(
        build_ooxml_zip("word/document.xml"),
        "word/document.xml",
        8,
        "<H",
        1,
    )
    path = write_bytes(tmp_path / "encrypted.bin", package)

    assert MediaDetector().detect(path) == "application/octet-stream"


@pytest.mark.parametrize(
    ("local_field_offset", "field_format", "value"),
    (
        (6, "<H", 0x0800),
        (8, "<H", zipfile.ZIP_DEFLATED),
        (18, "<L", 99),
        (22, "<L", 99),
    ),
)
def test_detector_rejects_incoherent_local_header_fields(
    tmp_path: Path, local_field_offset: int, field_format: str, value: int
) -> None:
    package = bytearray(build_ooxml_zip("word/document.xml"))
    local_offset = central_local_offset(package, "word/document.xml")
    struct.pack_into(field_format, package, local_offset + local_field_offset, value)
    path = write_bytes(tmp_path / "local-mismatch.bin", bytes(package))

    assert MediaDetector().detect(path) == "application/octet-stream"


@pytest.mark.skipif(not hasattr(os, "pread"), reason="requires positional reads")
def test_ooxml_local_header_reads_are_aggregate_bounded_and_skip_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = build_ooxml_zip("word/document.xml", first_body=b"x" * 70_000)
    path = write_bytes(tmp_path / "bounded-local-metadata.bin", package)
    body_spans = zip_member_body_spans(package)
    real_pread = os.pread
    requests: list[tuple[int, int]] = []

    def observing_pread(descriptor: int, size: int, offset: int) -> bytes:
        requests.append((offset, size))
        return real_pread(descriptor, size, offset)

    monkeypatch.setattr(os, "pread", observing_pread)

    assert MediaDetector().detect(path) == DOCX_MEDIA_TYPE
    assert requests
    assert sum(size for _offset, size in requests) <= 131_072
    assert all(
        request_offset + request_size <= body_start or request_offset >= body_end
        for request_offset, request_size in requests
        for body_start, body_end in body_spans
    )


def test_detector_prefers_valid_utf8_text_to_a_misleading_binary_extension(
    tmp_path: Path,
) -> None:
    path = write_bytes(tmp_path / "not-a-pdf.pdf", b"ordinary UTF-8 text\n")

    assert MediaDetector().detect(path) == "text/plain"


def test_detector_loops_over_forced_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_bytes(tmp_path / "binary.bin", b"abcdefg\xff")
    real_read = os.read
    requests: list[int] = []
    returned = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal returned
        requests.append(size)
        chunk = real_read(descriptor, min(size, 3))
        returned += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", short_read)

    assert MediaDetector().detect(path) == "application/octet-stream"
    assert max(requests) <= 65_537
    assert returned <= 65_537


def test_detector_retries_interrupted_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_bytes(tmp_path / "plain.txt", b"plain text")
    real_read = os.read
    interrupted = False

    def interrupted_once(descriptor: int, size: int) -> bytes:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", interrupted_once)

    assert MediaDetector().detect(path) == "text/plain"
    assert interrupted


def test_detector_treats_utf8_split_at_sample_boundary_as_text(tmp_path: Path) -> None:
    path = write_bytes(tmp_path / "split.bin", b"a" * 65_534 + b"\xe2\x82\xac")

    assert MediaDetector().detect(path) == "text/plain"


def test_detector_never_reads_more_than_the_prefix_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_bytes(tmp_path / "large.bin", b"\x00" * 100_000)
    real_read = os.read
    requests: list[int] = []
    returned = 0

    def observing_read(descriptor: int, size: int) -> bytes:
        nonlocal returned
        requests.append(size)
        chunk = real_read(descriptor, size)
        returned += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", observing_read)

    assert MediaDetector().detect(path) == "application/octet-stream"
    assert max(requests) <= 65_537
    assert returned == 65_537


@pytest.mark.parametrize(
    ("name", "expected"),
    (
        ("notes.md", "text/markdown"),
        ("page.html", "text/html"),
        ("data.csv", "text/csv"),
        ("data.tsv", "text/tab-separated-values"),
        ("data.json", "application/json"),
        ("data.ndjson", "application/x-ndjson"),
    ),
)
def test_detector_refines_valid_text_with_approved_extension_vocabulary(
    tmp_path: Path, name: str, expected: str
) -> None:
    path = write_bytes(tmp_path / name, b"valid UTF-8\n")

    assert MediaDetector().detect(path) == expected


def test_detector_recognizes_exact_url_descriptor_suffix_before_markdown() -> None:
    assert MediaDetector().detect(FIXTURES / "descriptor.url.md") == (
        URL_DESCRIPTOR_MEDIA_TYPE
    )


def test_inventory_keeps_byte_signature_precedence_for_url_named_file(
    repo_root: Path,
) -> None:
    write_bytes(repo_root / "sources/raw/not-a-descriptor.url.md", b"%PDF-1.7\n")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.skipped == ()
    assert report.items[0].media_type == "application/pdf"
    assert report.items[0].url_descriptor is None


def test_external_symlink_is_skipped(repo_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo_root / "sources/raw/outside.txt"
    link.symlink_to(outside)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == ()
    assert [diagnostic.path for diagnostic in report.skipped] == [
        PurePosixPath("outside.txt")
    ]


@pytest.mark.parametrize(
    "reserved", ("_versions/src_x/a/old.txt", "_web/src_x/a/page.html")
)
def test_symlink_to_reserved_in_tree_target_is_skipped(
    repo_root: Path, reserved: str
) -> None:
    target = write_bytes(repo_root / "sources/raw" / reserved, b"hidden")
    (repo_root / "sources/raw/alias.txt").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == ()
    assert [diagnostic.path for diagnostic in report.skipped] == [
        PurePosixPath("alias.txt")
    ]


def test_safe_in_tree_file_symlink_is_inventoried(repo_root: Path) -> None:
    target = write_bytes(repo_root / "sources/raw/notes/target.txt", b"target")
    (repo_root / "sources/raw/alias.txt").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    alias = next(
        item
        for item in report.items
        if item.fingerprint.path == PurePosixPath("alias.txt")
    )
    assert alias.fingerprint.byte_size == len(b"target")
    assert alias.media_type == "text/plain"


def test_symlink_to_legitimate_nested_reserved_name_is_inventoried(
    repo_root: Path,
) -> None:
    target = write_bytes(
        repo_root / "sources/raw/archive/_versions/target.txt", b"target"
    )
    (repo_root / "sources/raw/alias.txt").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert PurePosixPath("alias.txt") in {
        item.fingerprint.path for item in report.items
    }


def test_url_descriptor_symlink_uses_alias_suffix_not_target_suffix(
    repo_root: Path,
) -> None:
    target = write_bytes(
        repo_root / "sources/raw/targets/payload.bin",
        valid_descriptor_bytes(),
    )
    (repo_root / "sources/raw/logical.url.md").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    alias = item_at(report.items, "logical.url.md")
    assert alias.media_type == URL_DESCRIPTOR_MEDIA_TYPE
    assert alias.url_descriptor == UrlDescriptor(
        PurePosixPath("logical.url.md"),
        "https://example.test/a",
        "Example",
        date(2026, 9, 4),
    )


def test_non_descriptor_symlink_ignores_target_url_suffix(repo_root: Path) -> None:
    target = write_bytes(
        repo_root / "sources/raw/targets/payload.url.md",
        valid_descriptor_bytes(),
    )
    (repo_root / "sources/raw/logical.txt").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    alias = item_at(report.items, "logical.txt")
    assert alias.media_type == "text/plain"
    assert alias.url_descriptor is None


def test_binary_symlink_fallback_uses_alias_extension(repo_root: Path) -> None:
    target = write_bytes(repo_root / "sources/raw/targets/payload.bin", b"\x00\x01")
    (repo_root / "sources/raw/logical.pdf").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert item_at(report.items, "logical.pdf").media_type == "application/pdf"


def test_binary_symlink_does_not_inherit_target_extension(repo_root: Path) -> None:
    target = write_bytes(repo_root / "sources/raw/targets/payload.pdf", b"\x00\x01")
    (repo_root / "sources/raw/logical.bin").symlink_to(target)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert item_at(report.items, "logical.bin").media_type == "application/octet-stream"


def test_broken_symlink_becomes_a_deterministic_skip(repo_root: Path) -> None:
    (repo_root / "sources/raw/broken.txt").symlink_to("missing.txt")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == ()
    assert len(report.skipped) == 1
    assert report.skipped[0].path == PurePosixPath("broken.txt")


def test_detector_read_error_skips_one_file_without_losing_other_items(
    repo_root: Path,
) -> None:
    write_bytes(repo_root / "sources/raw/bad.txt", b"bad")
    write_bytes(repo_root / "sources/raw/good.txt", b"good")

    class OneReadFails(MediaDetector):
        def detect_open_file(self, descriptor: int, logical_name: str) -> str:
            if logical_name == "bad.txt":
                raise OSError("simulated read race")
            return super().detect_open_file(descriptor, logical_name)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), OneReadFails())

    assert [item.fingerprint.path for item in report.items] == [
        PurePosixPath("good.txt")
    ]
    assert [diagnostic.path for diagnostic in report.skipped] == [
        PurePosixPath("bad.txt")
    ]


def test_directory_swap_cannot_redirect_recursive_enumeration(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    nested = raw / "nested"
    saved = tmp_path / "saved-nested"
    outside = tmp_path / "outside"
    write_bytes(nested / "inside.txt", b"inside")
    write_bytes(raw / "good.txt", b"good")
    write_bytes(outside / "secret.txt", b"outside secret")
    original_iterdir = Path.iterdir
    original_open = os.open
    swapped = False

    def swap_directory() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        nested.rename(saved)
        nested.symlink_to(outside, target_is_directory=True)

    def swapping_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == nested:
            swap_directory()
        return original_iterdir(path)

    def swapping_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "nested" and flags & getattr(os, "O_DIRECTORY", 0):
            swap_directory()
        return call_os_open(original_open, path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "iterdir", swapping_iterdir)
    monkeypatch.setattr(os, "open", swapping_open)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert swapped
    assert [item.fingerprint.path for item in report.items] == [
        PurePosixPath("good.txt")
    ]
    assert all(
        item.fingerprint.path != PurePosixPath("nested/secret.txt")
        for item in report.items
    )


def test_ancestor_swap_cannot_redirect_content_open(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    nested = raw / "nested"
    saved = tmp_path / "saved-nested"
    outside = tmp_path / "outside"
    original = write_bytes(nested / "source.bin", b"%PDF-1.7\n")
    replacement = write_bytes(outside / "source.bin", b"\x89PNG\r\n\x1a\n")
    write_bytes(raw / "z-good.txt", b"good")
    original_inode = original.stat().st_ino
    replacement_inode = replacement.stat().st_ino
    real_open = os.open
    opened_inodes: list[int] = []
    swapped = False

    def swapping_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if Path(path).name == "source.bin" and not swapped:
            swapped = True
            nested.rename(saved)
            nested.symlink_to(outside, target_is_directory=True)
        descriptor = call_os_open(real_open, path, flags, mode, dir_fd=dir_fd)
        if Path(path).name == "source.bin":
            opened_inodes.append(os.fstat(descriptor).st_ino)
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert swapped
    assert original_inode in opened_inodes
    assert replacement_inode not in opened_inodes
    assert [item.fingerprint.path for item in report.items] == [
        PurePosixPath("z-good.txt")
    ]
    stale_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("nested/source.bin")
    )
    assert stale_diagnostic.code == "source_changed_during_inventory"


def test_symlink_target_intermediate_directory_move_is_rejected(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    targets = raw / "targets"
    moved_targets = tmp_path / "moved-targets"
    write_bytes(targets / "secret.txt", b"old in-tree content")
    (raw / "alias.txt").symlink_to("targets/secret.txt")
    real_stat = os.stat
    moved = False

    def moving_stat(
        path: os.PathLike[str] | str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal moved
        if path == "secret.txt" and dir_fd is not None and not moved:
            moved = True
            targets.rename(moved_targets)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", moving_stat)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert moved
    assert all(
        item.fingerprint.path != PurePosixPath("alias.txt") for item in report.items
    )
    alias_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("alias.txt")
    )
    assert alias_diagnostic.code == "source_changed_during_inventory"


def test_moved_symlink_target_directory_cannot_supply_new_external_content(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    targets = raw / "targets"
    moved_targets = tmp_path / "moved-targets"
    targets.mkdir()
    (raw / "alias.txt").symlink_to("targets/created-after-move.txt")
    real_stat = os.stat
    moved = False

    def moving_and_populating_stat(
        path: os.PathLike[str] | str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal moved
        if path == "created-after-move.txt" and dir_fd is not None and not moved:
            moved = True
            targets.rename(moved_targets)
            write_bytes(moved_targets / "created-after-move.txt", b"external content")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", moving_and_populating_stat)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert moved
    assert all(
        item.fingerprint.path != PurePosixPath("alias.txt") for item in report.items
    )
    alias_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("alias.txt")
    )
    assert alias_diagnostic.code == "source_changed_during_inventory"


def test_parent_of_parent_replacement_rejects_stale_descendant(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    ancestor = raw / "a"
    moved_ancestor = tmp_path / "moved-a"
    original = write_bytes(ancestor / "b/source.bin", b"%PDF-1.7\n")
    write_bytes(raw / "z-good.txt", b"good")
    original_inode = original.stat().st_ino
    real_open = os.open
    opened_inodes: list[int] = []
    replaced = False

    def replacing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "source.bin" and dir_fd is not None and not replaced:
            replaced = True
            ancestor.rename(moved_ancestor)
            write_bytes(ancestor / "b/source.bin", b"\x89PNG\r\n\x1a\n")
        descriptor = call_os_open(real_open, path, flags, mode, dir_fd=dir_fd)
        if path == "source.bin":
            opened_inodes.append(os.fstat(descriptor).st_ino)
        return descriptor

    monkeypatch.setattr(os, "open", replacing_open)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert replaced
    assert opened_inodes == [original_inode]
    assert [item.fingerprint.path for item in report.items] == [
        PurePosixPath("z-good.txt")
    ]
    stale_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("a/b/source.bin")
    )
    assert stale_diagnostic.code == "source_changed_during_inventory"


def test_raw_replacement_after_anchoring_rejects_old_item(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = repo_root / "sources/raw"
    moved_raw = tmp_path / "moved-raw"
    original = write_bytes(raw / "old.txt", b"old content")
    original_inode = original.stat().st_ino
    real_open = os.open
    opened_inodes: list[int] = []
    replaced = False

    def replacing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "old.txt" and dir_fd is not None and not replaced:
            replaced = True
            raw.rename(moved_raw)
            write_bytes(raw / "new.txt", b"new content")
        descriptor = call_os_open(real_open, path, flags, mode, dir_fd=dir_fd)
        if path == "old.txt":
            opened_inodes.append(os.fstat(descriptor).st_ino)
        return descriptor

    monkeypatch.setattr(os, "open", replacing_open)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert replaced
    assert opened_inodes == [original_inode]
    assert report.items == ()
    stale_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("old.txt")
    )
    assert stale_diagnostic.code == "source_changed_during_inventory"


def test_final_file_replacement_is_rejected_before_replacement_bytes_are_read(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write_bytes(repo_root / "sources/raw/victim.bin", b"%PDF-1.7\n")
    real_open = os.open
    real_read = os.read
    replaced = False
    replacement_inode: int | None = None
    read_inodes: list[int] = []

    def replacing_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced, replacement_inode
        if Path(path).name == "victim.bin" and not replaced:
            replaced = True
            source.unlink()
            source.write_bytes(b"\x89PNG\r\n\x1a\n")
            replacement_inode = source.stat().st_ino
        return call_os_open(real_open, path, flags, mode, dir_fd=dir_fd)

    def observing_read(descriptor: int, size: int) -> bytes:
        read_inodes.append(os.fstat(descriptor).st_ino)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "open", replacing_open)
    monkeypatch.setattr(os, "read", observing_read)

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert replaced and replacement_inode is not None
    assert replacement_inode not in read_inodes
    assert report.items == ()
    assert [diagnostic.path for diagnostic in report.skipped] == [
        PurePosixPath("victim.bin")
    ]


def test_symlink_target_replacement_during_read_invalidates_alias_item(
    repo_root: Path,
) -> None:
    target = write_bytes(repo_root / "sources/raw/targets/source.bin", b"%PDF-1.7\n")
    (repo_root / "sources/raw/alias.bin").symlink_to(target)
    replaced = False

    class TargetReplacingDetector(MediaDetector):
        def detect_open_file(self, descriptor: int, logical_name: str) -> str:
            nonlocal replaced
            if logical_name == "alias.bin" and not replaced:
                replaced = True
                target.unlink()
                target.write_bytes(b"\x89PNG\r\n\x1a\n")
            return super().detect_open_file(descriptor, logical_name)

    report = inventory_raw_sources(
        RepoPaths.discover(repo_root), TargetReplacingDetector()
    )

    assert replaced
    assert all(
        item.fingerprint.path != PurePosixPath("alias.bin") for item in report.items
    )
    alias_diagnostic = next(
        diagnostic
        for diagnostic in report.skipped
        if diagnostic.path == PurePosixPath("alias.bin")
    )
    assert alias_diagnostic.code == "source_changed_during_inventory"


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires POSIX FIFO and fork isolation",
)
def test_fifo_replacement_is_rejected_without_blocking(
    repo_root: Path,
) -> None:
    source = write_bytes(repo_root / "sources/raw/victim.txt", b"regular")
    context = multiprocessing.get_context("fork")
    results = context.Queue()

    def run_inventory() -> None:
        real_open = os.open
        replaced = False

        def replacing_open(
            path: os.PathLike[str] | str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if Path(path).name == "victim.txt" and not replaced:
                replaced = True
                source.unlink()
                os.mkfifo(source)
            return call_os_open(real_open, path, flags, mode, dir_fd=dir_fd)

        inventory_module.os.open = replacing_open
        report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())
        results.put(
            (
                tuple(item.fingerprint.path.as_posix() for item in report.items),
                tuple(
                    diagnostic.path.as_posix()
                    for diagnostic in report.skipped
                    if diagnostic.path is not None
                ),
            )
        )

    process = context.Process(target=run_inventory)
    process.start()
    process.join(timeout=2)
    try:
        assert not process.is_alive(), "inventory blocked opening a replacement FIFO"
        assert process.exitcode == 0
        assert results.get(timeout=1) == ((), ("victim.txt",))
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        results.close()
        results.join_thread()


def test_inventory_refuses_to_weaken_safety_without_required_fd_primitives(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_bytes(repo_root / "sources/raw/notes/a.txt", b"visible")
    monkeypatch.setattr(inventory_module.os, "supports_dir_fd", set())

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == ()
    assert [diagnostic.code for diagnostic in report.skipped] == [
        "source_inventory_unsupported"
    ]


def test_url_descriptor_is_control_metadata_without_network(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "create_connection", pytest.fail)
    descriptor = repo_root / "sources/raw/urls/example.url.md"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        "---\nkind: url\nurl: https://example.test/a\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        encoding="utf-8",
    )
    observed = descriptor.stat()

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.skipped == ()
    assert report.items[0].url_descriptor == UrlDescriptor(
        PurePosixPath("urls/example.url.md"),
        "https://example.test/a",
        "Example",
        date(2026, 9, 4),
    )
    assert report.items[0].media_type == URL_DESCRIPTOR_MEDIA_TYPE
    assert report.items[0].extension == ".url.md"
    assert report.items[0].sha256 is None
    assert report.items[0].fingerprint.byte_size == observed.st_size
    assert report.items[0].fingerprint.mtime_ns == observed.st_mtime_ns


def test_url_descriptor_decodes_canonical_json_string_scalars(
    repo_root: Path,
) -> None:
    descriptor = repo_root / "sources/raw/urls/quoted.url.md"
    descriptor.parent.mkdir(parents=True)
    url = "https://example.test/a:b?q=%5Bvalue%5D#café"
    description = 'Colon: hash # quotes "hello" brackets [x] Unicode 雪'
    descriptor.write_text(
        "---\nkind: url\n"
        f"url: {json.dumps(url, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "added: 2026-09-04\n---\n",
        encoding="utf-8",
    )

    assert parse_url_descriptor(
        descriptor, PurePosixPath("urls/quoted.url.md")
    ) == UrlDescriptor(
        PurePosixPath("urls/quoted.url.md"),
        url,
        description,
        date(2026, 9, 4),
    )


def test_descriptor_short_reads_cannot_hide_oversized_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = valid_descriptor_bytes()
    descriptor = write_bytes(tmp_path / "oversized.url.md", prefix + b"x" * 70_000)
    real_read = os.read

    def prefix_sized_reads(file_descriptor: int, size: int) -> bytes:
        return real_read(file_descriptor, min(size, len(prefix)))

    monkeypatch.setattr(os, "read", prefix_sized_reads)

    with pytest.raises(ValueError, match="byte limit"):
        parse_url_descriptor(descriptor, PurePosixPath("oversized.url.md"))


def test_descriptor_at_exact_byte_limit_is_accepted(tmp_path: Path) -> None:
    descriptor = write_bytes(
        tmp_path / "exact.url.md",
        descriptor_with_size(65_536),
    )

    assert (
        parse_url_descriptor(descriptor, PurePosixPath("exact.url.md")).url
        == "https://example.test/a"
    )


@pytest.mark.parametrize("size", (65_537, 200_000))
def test_descriptor_beyond_byte_limit_is_rejected(tmp_path: Path, size: int) -> None:
    descriptor = write_bytes(
        tmp_path / "oversized.url.md",
        descriptor_with_size(size),
    )

    with pytest.raises(ValueError, match="byte limit"):
        parse_url_descriptor(descriptor, PurePosixPath("oversized.url.md"))


def test_checked_in_url_descriptor_fixture_round_trips() -> None:
    descriptor = parse_url_descriptor(
        FIXTURES / "descriptor.url.md", PurePosixPath("urls/descriptor.url.md")
    )

    assert descriptor == UrlDescriptor(
        PurePosixPath("urls/descriptor.url.md"),
        "https://example.test/a:b?q=%5Bvalue%5D#café",
        'Colon: hash # quotes "hello" brackets [x] Unicode 雪',
        date(2026, 9, 4),
    )


@pytest.mark.parametrize(
    "document",
    (
        "---\nkind: url\nkind: url\nurl: https://example.test\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test\ndescription: Example\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        "description: Example\nadded: 2026-09-04\nextra: no\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        "description: |\n  multiline\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        "description: [collection]\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        "description: 'single quoted'\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        "description: ambiguous: scalar\nadded: 2026-09-04\n---\n",
        '---\nkind: url\nurl: "https://example.test\n'
        "description: Example\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test\n"
        'description: "line\\nbreak"\nadded: 2026-09-04\n---\n',
        "---\nkind: url\nurl: https://example.test\n"
        "description: Example\nadded: 2026-02-30\n---\n",
        "---\nkind: url\nurl: ftp://example.test\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        "---\nkind: url\nurl: https://example.test/a path\n"
        "description: Example\nadded: 2026-09-04\n---\n",
    ),
)
def test_invalid_url_descriptors_are_reported_and_skipped(
    repo_root: Path, document: str
) -> None:
    descriptor = repo_root / "sources/raw/urls/invalid.url.md"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(document, encoding="utf-8")

    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())

    assert report.items == ()
    assert [diagnostic.code for diagnostic in report.skipped] == [
        "invalid_url_descriptor"
    ]
    assert report.skipped[0].path == PurePosixPath("urls/invalid.url.md")


def test_url_descriptor_rejects_non_frontmatter_content(tmp_path: Path) -> None:
    descriptor = tmp_path / "not-frontmatter.url.md"
    descriptor.write_text("kind: url\nurl: https://example.test\n", encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter"):
        parse_url_descriptor(descriptor, PurePosixPath("not-frontmatter.url.md"))


def test_source_id_for_url_descriptor_uses_path_and_url_identity() -> None:
    descriptor = UrlDescriptor(
        PurePosixPath("urls/example.url.md"),
        "https://example.test/a",
        "Description does not affect identity",
        date(2026, 9, 4),
    )
    descriptor_checksum = hashlib.sha256(
        b"url-descriptor-v1\0https://example.test/a"
    ).hexdigest()

    assert source_id_for_url_descriptor(descriptor) == source_id_for_first_seen(
        PurePosixPath("urls/example.url.md"), descriptor_checksum
    )


def write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def valid_descriptor_bytes() -> bytes:
    return (
        b"---\nkind: url\nurl: https://example.test/a\n"
        b"description: Example\nadded: 2026-09-04\n---\n"
    )


def descriptor_with_size(size: int) -> bytes:
    prefix = valid_descriptor_bytes()
    assert size >= len(prefix)
    return prefix + b"x" * (size - len(prefix))


def build_ooxml_zip(
    family_member: str,
    *,
    first_body: bytes | None = None,
    first_member_name: str = "padding.bin",
    family_extra: bytes = b"",
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as package:
        if first_body is not None or first_member_name != "padding.bin":
            write_zip_member(package, first_member_name, first_body or b"")
        write_zip_member(package, "[Content_Types].xml", b"<Types/>")
        write_zip_member(package, "_rels/.rels", b"<Relationships/>")
        write_zip_member(
            package,
            family_member,
            b"<document/>",
            extra=family_extra,
        )
        for name, content in extra_members:
            write_zip_member(package, name, content)
    return buffer.getvalue()


def write_zip_member(
    package: zipfile.ZipFile,
    name: str,
    content: bytes,
    *,
    extra: bytes = b"",
    compression: int = zipfile.ZIP_STORED,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.extra = extra
    package.writestr(info, content)


def build_deflated_ooxml_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        write_zip_member(
            package,
            "[Content_Types].xml",
            b"<Types/>",
            compression=zipfile.ZIP_DEFLATED,
        )
        write_zip_member(
            package,
            "_rels/.rels",
            b"<Relationships/>",
            compression=zipfile.ZIP_DEFLATED,
        )
        write_zip_member(
            package,
            "word/document.xml",
            b"<document/>",
            compression=zipfile.ZIP_DEFLATED,
        )
    return buffer.getvalue()


def build_force_zip64_ooxml_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, content in (
            ("[Content_Types].xml", b"<Types/>"),
            ("_rels/.rels", b"<Relationships/>"),
            ("word/document.xml", b"<document/>"),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with package.open(info, mode="w", force_zip64=True) as member:
                member.write(content)
    return buffer.getvalue()


def central_record_offset(package: bytes | bytearray, member_name: str) -> int:
    eocd_offset = package.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    cursor = struct.unpack_from("<L", package, eocd_offset + 16)[0]
    member_count = struct.unpack_from("<H", package, eocd_offset + 10)[0]
    for _index in range(member_count):
        assert package[cursor : cursor + 4] == b"PK\x01\x02"
        name_size, extra_size, comment_size = struct.unpack_from(
            "<3H", package, cursor + 28
        )
        name = bytes(package[cursor + 46 : cursor + 46 + name_size]).decode("ascii")
        if name == member_name:
            return cursor
        cursor += 46 + name_size + extra_size + comment_size
    raise AssertionError(f"missing central member: {member_name}")


def central_local_offset(package: bytes | bytearray, member_name: str) -> int:
    central_offset = central_record_offset(package, member_name)
    return struct.unpack_from("<L", package, central_offset + 42)[0]


def mutate_central_field(
    package: bytes,
    member_name: str,
    field_offset: int,
    field_format: str,
    value: int,
) -> bytes:
    mutated = bytearray(package)
    central_offset = central_record_offset(mutated, member_name)
    struct.pack_into(field_format, mutated, central_offset + field_offset, value)
    return bytes(mutated)


def zip_member_body_spans(package: bytes) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        for info in archive.infolist():
            name_size, extra_size = struct.unpack_from(
                "<2H", package, info.header_offset + 26
            )
            body_start = info.header_offset + 30 + name_size + extra_size
            spans.append((body_start, body_start + info.compress_size))
    return tuple(spans)


def item_at(items: tuple[InventoryItem, ...], path: str) -> InventoryItem:
    return next(item for item in items if item.fingerprint.path == PurePosixPath(path))


def call_os_open(
    function,  # type: ignore[no-untyped-def]
    path: os.PathLike[str] | str,
    flags: int,
    mode: int,
    *,
    dir_fd: int | None,
) -> int:
    if dir_fd is None:
        return function(path, flags, mode)
    return function(path, flags, mode, dir_fd=dir_fd)


def test_stable_snapshot_allows_safe_raw_user_alias_but_rejects_evidence_alias(
    repo_root: Path,
) -> None:
    target = write_bytes(repo_root / "sources/raw/notes/target.txt", b"safe")
    alias = repo_root / "sources/raw/notes/alias.txt"
    alias.symlink_to(target.name)
    paths = RepoPaths.discover(repo_root)
    namespace = getattr(inventory_module, "SnapshotNamespace")
    snapshot = inventory_module.stable_file_snapshot(
        paths,
        namespace.RAW_USER,
        PurePosixPath("notes/alias.txt"),
        include_sha256=True,
    )

    assert snapshot.byte_size == 4
    assert snapshot.sha256 == hashlib.sha256(b"safe").hexdigest()

    source_id = "src_" + "a" * 64
    checksum = "b" * 64
    archived = write_bytes(
        repo_root / "sources/raw/_versions" / source_id / checksum / "file.txt",
        b"history",
    )
    archived_alias = archived.with_name("alias.txt")
    archived_alias.symlink_to(archived.name)
    with pytest.raises(inventory_module.InventoryAccessError):
        inventory_module.stable_file_snapshot(
            paths,
            namespace.RAW_VERSION,
            PurePosixPath("_versions", source_id, checksum, "alias.txt"),
        )


@pytest.mark.parametrize(
    ("namespace_name", "logical_path", "repository_path"),
    (
        (
            "RAW_WEB",
            PurePosixPath("_web", "src_" + "a" * 64, "b" * 64, "page.html"),
            PurePosixPath("sources/raw/_web", "src_" + "a" * 64, "b" * 64, "page.html"),
        ),
        (
            "EXTRACTED",
            PurePosixPath(
                "sources/extracted/notes/a.txt",
                "b" * 64,
                "drv_" + "c" * 64 + ".md",
            ),
            PurePosixPath(
                "sources/extracted/notes/a.txt",
                "b" * 64,
                "drv_" + "c" * 64 + ".md",
            ),
        ),
    ),
)
def test_stable_snapshot_supports_regular_web_and_extracted_evidence(
    repo_root: Path,
    namespace_name: str,
    logical_path: PurePosixPath,
    repository_path: PurePosixPath,
) -> None:
    evidence = write_bytes(repo_root / repository_path, b"evidence")
    namespace = getattr(inventory_module, "SnapshotNamespace")

    snapshot = inventory_module.stable_file_snapshot(
        RepoPaths.discover(repo_root),
        getattr(namespace, namespace_name),
        logical_path,
        include_sha256=True,
    )

    assert snapshot.byte_size == evidence.stat().st_size
    assert snapshot.mtime_ns == evidence.stat().st_mtime_ns
    assert snapshot.sha256 == hashlib.sha256(b"evidence").hexdigest()


def test_stable_snapshot_passes_only_pinned_descriptor_path_to_hash_callback(
    repo_root: Path,
) -> None:
    write_bytes(repo_root / "sources/raw/notes/a.txt", b"pinned")
    paths = RepoPaths.discover(repo_root)
    namespace = getattr(inventory_module, "SnapshotNamespace")
    observed: list[Path] = []

    def inspect(path: Path) -> str:
        observed.append(path)
        assert str(path).startswith(("/dev/fd/", "/proc/self/fd/"))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    snapshot = inventory_module.stable_file_snapshot(
        paths,
        namespace.RAW_USER,
        PurePosixPath("notes/a.txt"),
        include_sha256=True,
        hash_file=inspect,
    )

    assert snapshot.sha256 == hashlib.sha256(b"pinned").hexdigest()
    assert len(observed) == 1


def test_stable_snapshot_detects_final_swap_without_hashing_outside_bytes(
    repo_root: Path,
) -> None:
    source = write_bytes(repo_root / "sources/raw/notes/a.txt", b"inside")
    sentinel = write_bytes(repo_root.parent / "outside-secret.txt", b"secret")
    paths = RepoPaths.discover(repo_root)
    namespace = getattr(inventory_module, "SnapshotNamespace")
    observed = b""

    def swap_then_hash(pinned: Path) -> str:
        nonlocal observed
        source.unlink()
        source.symlink_to(sentinel)
        observed = pinned.read_bytes()
        return hashlib.sha256(observed).hexdigest()

    with pytest.raises(inventory_module.InventoryAccessError):
        inventory_module.stable_file_snapshot(
            paths,
            namespace.RAW_USER,
            PurePosixPath("notes/a.txt"),
            include_sha256=True,
            hash_file=swap_then_hash,
        )

    assert observed == b"inside"


def test_stable_snapshot_detects_parent_swap_while_hashing_pinned_bytes(
    repo_root: Path,
) -> None:
    parent = repo_root / "sources/raw/notes"
    moved = repo_root.parent / "detached-notes"
    write_bytes(parent / "a.txt", b"original")
    paths = RepoPaths.discover(repo_root)
    namespace = getattr(inventory_module, "SnapshotNamespace")
    observed = b""

    def swap_parent_then_hash(pinned: Path) -> str:
        nonlocal observed
        parent.rename(moved)
        write_bytes(parent / "a.txt", b"replacement")
        observed = pinned.read_bytes()
        return hashlib.sha256(observed).hexdigest()

    with pytest.raises(inventory_module.InventoryAccessError):
        inventory_module.stable_file_snapshot(
            paths,
            namespace.RAW_USER,
            PurePosixPath("notes/a.txt"),
            include_sha256=True,
            hash_file=swap_parent_then_hash,
        )

    assert observed == b"original"


def test_stable_snapshot_balances_descriptors_when_hash_callback_interrupts(
    repo_root: Path,
) -> None:
    descriptor_root = next(
        (
            candidate
            for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
            if candidate.is_dir()
        ),
        None,
    )
    if descriptor_root is None:
        pytest.skip("descriptor count is not observable")
    write_bytes(repo_root / "sources/raw/a.txt", b"value")
    before = len(tuple(descriptor_root.iterdir()))
    namespace = getattr(inventory_module, "SnapshotNamespace")

    def interrupt(_path: Path) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        inventory_module.stable_file_snapshot(
            RepoPaths.discover(repo_root),
            namespace.RAW_USER,
            PurePosixPath("a.txt"),
            include_sha256=True,
            hash_file=interrupt,
        )

    assert len(tuple(descriptor_root.iterdir())) == before


def test_stable_snapshot_rejects_invalid_syntax_before_filesystem_io(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = getattr(inventory_module, "SnapshotNamespace")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("filesystem I/O happened before path validation")

    monkeypatch.setattr(inventory_module, "_open_snapshot_root", forbidden)
    with pytest.raises(ValueError):
        inventory_module.stable_file_snapshot(
            RepoPaths.discover(repo_root),
            namespace.EXTRACTED,
            PurePosixPath("../outside"),
        )


@pytest.mark.parametrize(
    ("namespace_name", "path"),
    (
        ("RAW_VERSION", PurePosixPath("_versions/src_x/checksum/a.txt")),
        ("RAW_WEB", PurePosixPath("_web/src_x/checksum/a.txt")),
        ("EXTRACTED", PurePosixPath("sources/extracted/not-an-artifact")),
    ),
)
def test_stable_snapshot_rejects_malformed_evidence_identity_before_io(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    namespace_name: str,
    path: PurePosixPath,
) -> None:
    namespace = getattr(inventory_module, "SnapshotNamespace")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("filesystem I/O happened before identity validation")

    monkeypatch.setattr(inventory_module, "_open_snapshot_root", forbidden)
    with pytest.raises(ValueError):
        inventory_module.stable_file_snapshot(
            RepoPaths.discover(repo_root),
            getattr(namespace, namespace_name),
            path,
        )


def test_stable_snapshot_balances_descriptors_on_identity_allocation_failure(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor_root = next(
        (
            candidate
            for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
            if candidate.is_dir()
        ),
        None,
    )
    if descriptor_root is None:
        pytest.skip("descriptor count is not observable")
    write_bytes(repo_root / "sources/raw/a.txt", b"value")
    before = len(tuple(descriptor_root.iterdir()))
    namespace = getattr(inventory_module, "SnapshotNamespace")

    def fail_identity(*_args: object, **_kwargs: object) -> object:
        raise MemoryError("allocation failed")

    monkeypatch.setattr(inventory_module, "_DirectoryEdge", fail_identity)
    with pytest.raises(MemoryError):
        inventory_module.stable_file_snapshot(
            RepoPaths.discover(repo_root),
            namespace.RAW_USER,
            PurePosixPath("a.txt"),
        )

    assert len(tuple(descriptor_root.iterdir())) == before


def test_stable_snapshot_balances_descriptors_on_snapshot_allocation_failure(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor_root = next(
        (
            candidate
            for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
            if candidate.is_dir()
        ),
        None,
    )
    if descriptor_root is None:
        pytest.skip("descriptor count is not observable")
    write_bytes(repo_root / "sources/raw/a.txt", b"value")
    before = len(tuple(descriptor_root.iterdir()))
    namespace = getattr(inventory_module, "SnapshotNamespace")

    def fail_snapshot(*_args: object, **_kwargs: object) -> object:
        raise MemoryError("allocation failed")

    monkeypatch.setattr(inventory_module, "_ResolvedContent", fail_snapshot)
    with pytest.raises(MemoryError):
        inventory_module.stable_file_snapshot(
            RepoPaths.discover(repo_root),
            namespace.RAW_USER,
            PurePosixPath("a.txt"),
        )

    assert len(tuple(descriptor_root.iterdir())) == before
