"""Literal, ledger-scoped ripgrep search and authenticated evidence contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .contracts import compute_corpus_revision
from .diagnostics import Diagnostic
from .evidence import SearchPageRecord, SearchPassName, SearchPassRecord
from .layout import RepoPaths
from .ledger import LedgerStore, _PinnedDirectory

SearchScope = Literal["wiki", "sources"]
SearchPhase = Literal["filenames", "context"]
SearchMode = Literal["research", "freshness"]
MAX_RG_PATHS = 256
MAX_RG_ARGV_BYTES = 131_072
MAX_RG_EVENT_BYTES = 65_536
MAX_PAGE_SIZE = 500
SEARCH_RUN_TTL = timedelta(hours=24)
_UNSAFE_WIKI_FILENAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class SearchError(RuntimeError):
    code = "search_failed"


class SearchArgumentLimitError(SearchError):
    code = "search_argument_limit"


class SearchOperandError(SearchError):
    code = "search_operand_invalid"


class SearchOutputLimitError(SearchError):
    code = "search_output_limit"


class SearchExecutionError(SearchError):
    code = "rg_failed"


class RgUnavailable(SearchExecutionError):
    code = "rg_unavailable"


class InvalidSearchCursor(SearchError):
    code = "invalid_search_cursor"


class SearchRunBlocked(SearchError):
    code = "search_run_incomplete"

    def __init__(self, message: str, *, diagnostic: Diagnostic | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic or Diagnostic(self.code, message)


class SearchRunStale(SearchRunBlocked):
    code = "search_run_stale"


class SearchRunExpired(SearchRunBlocked):
    code = "search_run_expired"


@dataclass(frozen=True)
class SearchRequest:
    scope: SearchScope
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    context_lines: int
    page_size: int = 100
    max_run_bytes: int = 1_073_741_824
    freshness_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scope not in {"wiki", "sources"}:
            raise ValueError("scope must be wiki or sources")
        if not isinstance(self.terms, tuple) or not self.terms:
            raise ValueError(
                "search requires at least one ordered-unique nonempty term"
            )
        for term in self.terms:
            if not isinstance(term, str) or not term.strip():
                raise ValueError("search terms must be nonempty strings")
            if any(character in term for character in "\r\n\0"):
                raise ValueError("search terms cannot contain CR, LF, or NUL")
            try:
                term.encode("utf-8")
            except UnicodeError as error:
                raise ValueError("search terms must be UTF-8") from error
        if len(set(self.terms)) != len(self.terms):
            raise ValueError("search terms must be ordered-unique")
        for name, value, low, high in (
            ("context_lines", self.context_lines, 0, 20),
            ("page_size", self.page_size, 1, MAX_PAGE_SIZE),
            ("max_run_bytes", self.max_run_bytes, 256, None),
        ):
            if (
                type(value) is not int
                or value < low
                or (high is not None and value > high)
            ):
                raise ValueError(f"{name} is outside its supported range")
        ids = self.freshness_source_ids
        if (
            not isinstance(ids, tuple)
            or any(
                not isinstance(item, str) or not re.fullmatch(r"src_[0-9a-f]{64}", item)
                for item in ids
            )
            or ids != tuple(sorted(set(ids)))
        ):
            raise ValueError(
                "freshness source IDs must be sorted unique full source IDs"
            )
        if self.scope == "wiki":
            if self.pass_name is not None or ids:
                raise ValueError("wiki search forbids a pass and freshness source IDs")
        elif ids:
            if self.pass_name is not None:
                raise ValueError("freshness searches cannot set a research pass")
        elif self.pass_name not in {"discovery", "expansion", "verification"}:
            raise ValueError("source research requires exactly one named pass")


@dataclass(frozen=True)
class SearchMatch:
    path: PurePosixPath
    line_number: int
    text: str
    kind: Literal["match", "context"]


@dataclass(frozen=True)
class SearchResult:
    run_id: str
    corpus_revision: str
    scope: SearchScope
    mode: SearchMode
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    page_index: int
    request_cursor: str | None
    next_cursor: str | None
    complete: bool
    candidate_count: int
    candidate_manifest: PurePosixPath
    candidate_manifest_sha256: str
    result_sha256: str
    matches: tuple[SearchMatch, ...]
    searched_source_ids: tuple[str, ...]
    coverage_gaps: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class SearchRunProof:
    run_id: str
    corpus_revision: str
    scope: SearchScope
    mode: SearchMode
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    candidate_count: int
    candidate_manifest_sha256: str
    page_count: int
    match_count: int
    page_index_sha256: str


def canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def match_record(match: SearchMatch) -> dict:
    return {
        "path": match.path.as_posix(),
        "line_number": match.line_number,
        "text": match.text,
        "kind": match.kind,
    }


def _page_index_record(
    page_index: int, start: int, end: int, record_count: int, checksum: str
) -> dict:
    """Canonical page identity shared by retained spooling and proof creation."""
    return {
        "page_index": page_index,
        "start": start,
        "end": end,
        "record_count": record_count,
        "sha256": checksum,
    }


def batch_paths(
    paths: tuple[Path, ...], *, fixed_argv: tuple[str, ...]
) -> tuple[tuple[Path, ...], ...]:
    fixed_size = sum(len(os.fsencode(argument)) + 1 for argument in fixed_argv)
    if fixed_size > MAX_RG_ARGV_BYTES:
        raise SearchArgumentLimitError("fixed rg arguments exceed the argv byte budget")
    batches, current = [], []
    size = fixed_size
    for path in sorted(paths, key=lambda item: item.as_posix()):
        cost = len(os.fsencode(path)) + 1
        if fixed_size + cost > MAX_RG_ARGV_BYTES:
            raise SearchArgumentLimitError(
                "one rg operand exceeds the argv byte budget"
            )
        if len(current) == MAX_RG_PATHS or size + cost > MAX_RG_ARGV_BYTES:
            batches.append(tuple(current))
            current = []
            size = fixed_size
        current.append(path)
        size += cost
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def build_rg_argv(
    request: SearchRequest,
    *,
    phase: SearchPhase,
    pattern_file: Path,
    paths: tuple[Path, ...] = (),
) -> list[str]:
    if phase == "filenames":
        flags = ["--fixed-strings", "--files-with-matches", "--null"]
    elif phase == "context":
        flags = [
            "--json",
            "--fixed-strings",
            "--line-number",
            "--context",
            str(request.context_lines),
        ]
    else:
        raise ValueError("invalid rg phase")
    return [
        "rg",
        "--no-config",
        "--sort",
        "path",
        *flags,
        "--file",
        str(pattern_file),
        "--",
        *(str(path) for path in paths),
    ]


def _regular_identity(
    path: Path, *, hash_content: bool = False
) -> tuple[int, int, str]:
    """Observe an operand without following any directory or final-file link."""
    with _PinnedDirectory.open(path.parent) as parent:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent.descriptor,
        )
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SearchOperandError(
                    "search operand must be a regular nonsymlink file"
                )
            digest = hashlib.sha256()
            if hash_content:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            after = os.fstat(stream.fileno())
            named = os.stat(path.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or (named.st_dev, named.st_ino, named.st_mode) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
            ):
                raise SearchOperandError("search operand changed while being observed")
            parent.validate()
            return before.st_size, before.st_mtime_ns, digest.hexdigest()


def bind_operands(
    paths: RepoPaths,
    ledger: LedgerStore,
    request: SearchRequest,
    *,
    retain_operands: bool = True,
) -> tuple[
    str,
    str,
    tuple[Path, ...],
    tuple[str, ...],
    tuple[tuple[PurePosixPath, str], ...],
]:
    revision = compute_corpus_revision(ledger.load_all().values())
    digest = hashlib.sha256()
    operands = []
    seen_paths = set()
    source_ids: tuple[str, ...] = ()
    wiki_content_sha256s: list[tuple[PurePosixPath, str]] = []
    try:
        if request.scope == "sources":
            if paths.extracted != paths.root / "sources/extracted":
                raise SearchOperandError("extracted root must be sources/extracted")
            selected = sorted(
                ledger.active_representations(), key=lambda item: item.source_id
            )
            requested = set(request.freshness_source_ids)
            if requested:
                selected = [item for item in selected if item.source_id in requested]
                if {item.source_id for item in selected} != requested:
                    raise SearchOperandError(
                        "freshness source ID has no active representation"
                    )
            source_ids = tuple(item.source_id for item in selected)
            if len(set(source_ids)) != len(source_ids):
                raise SearchOperandError("active source IDs must be unique")
            for item in selected:
                relative = item.extracted_path
                if (
                    relative.is_absolute()
                    or relative.parts[:2] != ("sources", "extracted")
                    or ".." in relative.parts
                ):
                    raise SearchOperandError(
                        "active representation is outside sources/extracted"
                    )
                if relative in seen_paths:
                    raise SearchOperandError(
                        "active representation paths must be unique"
                    )
                seen_paths.add(relative)
                operand = paths.root / relative
                try:
                    _regular_identity(operand)
                except FileNotFoundError as error:
                    raise SearchOperandError("missing active representation") from error
                if retain_operands:
                    operands.append(operand)
                digest.update(
                    canonical(
                        (
                            item.source_id,
                            item.content_sha256,
                            item.derivation_id,
                            relative.as_posix(),
                            item.output_sha256,
                        )
                    )
                )
        else:
            for directory, expected in (
                (paths.wiki_pages, "wiki/pages"),
                (paths.wiki_questions, "wiki/questions"),
            ):
                if directory != paths.root / expected:
                    raise SearchOperandError(
                        "wiki root must use canonical repository paths"
                    )
                with _PinnedDirectory.open(directory) as parent:
                    for name in sorted(os.listdir(parent.descriptor)):
                        if not name.endswith(".md") or not stat.S_ISREG(
                            os.stat(
                                name, dir_fd=parent.descriptor, follow_symlinks=False
                            ).st_mode
                        ):
                            continue
                        operand = directory / name
                        size, mtime, checksum = _regular_identity(
                            operand, hash_content=True
                        )
                        logical = PurePosixPath(
                            operand.relative_to(paths.root).as_posix()
                        )
                        if retain_operands:
                            operands.append(operand)
                        wiki_content_sha256s.append((logical, checksum))
                        digest.update(
                            canonical(
                                (
                                    logical.as_posix(),
                                    size,
                                    mtime,
                                    checksum,
                                )
                            )
                        )
                    parent.validate()
    except (OSError, ValueError) as error:
        raise SearchOperandError(
            f"unsafe or unavailable search operand: {error}"
        ) from error
    # Path comparisons cache a second component array per operand on Python
    # 3.14. Sort by the path string to keep the complete operand list bounded.
    return (
        revision,
        digest.hexdigest(),
        tuple(sorted(operands, key=str)),
        source_ids,
        tuple(wiki_content_sha256s),
    )


def is_canonical_wiki_record_name(name: object) -> bool:
    """Return whether a direct filename is safe in the logical wiki grammar."""

    if not isinstance(name, str):
        return False
    try:
        name.encode("utf-8")
    except UnicodeError:
        return False
    return (
        name.endswith(".md")
        and name != ".md"
        and "/" not in name
        and "\\" not in name
        and "\0" not in name
        and not any(
            unicodedata.category(character) in _UNSAFE_WIKI_FILENAME_CATEGORIES
            for character in name
        )
    )


def canonical_wiki_logical_path(
    paths: RepoPaths,
    page_path: Path | PurePosixPath | str,
    *,
    allow_absent: bool,
) -> PurePosixPath:
    """Require one direct, canonical page or question path.

    Candidate runs also bind a staged-yet-absent record, so existence is a
    separate rule from lexical canonicality. Existing targets are still
    observed without following their final path component.
    """

    if paths.wiki_pages != paths.root / "wiki/pages" or paths.wiki_questions != paths.root / "wiki/questions":
        raise SearchOperandError("wiki roots must use canonical repository paths")
    if isinstance(page_path, Path):
        if page_path.is_absolute():
            try:
                text = page_path.relative_to(paths.root).as_posix()
            except ValueError as error:
                raise ValueError("logical wiki path is outside the repository") from error
        else:
            text = page_path.as_posix()
    elif isinstance(page_path, PurePosixPath):
        text = page_path.as_posix()
    elif isinstance(page_path, str):
        text = page_path
    else:
        raise ValueError("logical wiki path must be a path")
    if not text or "\\" in text or "\0" in text:
        raise ValueError("logical wiki path must be canonical POSIX text")
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != text
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or len(candidate.parts) != 3
        or candidate.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")}
        or not is_canonical_wiki_record_name(candidate.name)
    ):
        raise ValueError("logical wiki path must name one direct page or question")
    parent = paths.root / candidate.parts[0] / candidate.parts[1]
    try:
        with _PinnedDirectory.open(parent) as pinned:
            try:
                observed = os.stat(
                    candidate.name,
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not allow_absent:
                    raise ValueError("logical wiki path is absent") from None
            else:
                if not stat.S_ISREG(observed.st_mode):
                    raise SearchOperandError(
                        "logical wiki path must be a regular nonsymlink file"
                    )
                _regular_identity(paths.root / candidate, hash_content=True)
            pinned.validate()
    except (OSError, ValueError) as error:
        if isinstance(error, SearchOperandError):
            raise
        raise SearchOperandError(f"unsafe logical wiki path: {error}") from error
    return candidate


def bind_link_candidate_operands(
    paths: RepoPaths,
    ledger: LedgerStore,
    excluded_page_path: Path | PurePosixPath | str,
    *,
    retain_operands: bool = True,
) -> tuple[str, str, tuple[Path, ...], tuple[str, ...], PurePosixPath]:
    """Bind every safe logical wiki record, including an excluded target.

    The `rg` operand list omits the target, while the identity intentionally
    contains it (when live) and the canonical intended-path marker. This lets
    a retained run cover both edits to the target and staged new records.
    """

    excluded = canonical_wiki_logical_path(
        paths, excluded_page_path, allow_absent=True
    )
    revision = compute_corpus_revision(ledger.load_all().values())
    records: list[tuple[PurePosixPath, Path, int, int, str]] = []
    try:
        for directory, prefix in (
            (paths.wiki_pages, ("wiki", "pages")),
            (paths.wiki_questions, ("wiki", "questions")),
        ):
            if directory != paths.root / "/".join(prefix):
                raise SearchOperandError("wiki root must use canonical repository paths")
            with _PinnedDirectory.open(directory) as parent:
                for name in sorted(os.listdir(parent.descriptor)):
                    observed = os.stat(
                        name, dir_fd=parent.descriptor, follow_symlinks=False
                    )
                    if name == ".gitkeep" and stat.S_ISREG(observed.st_mode):
                        continue
                    if not is_canonical_wiki_record_name(name):
                        raise SearchOperandError(
                            "wiki record tree contains a non-logical entry"
                        )
                    if not stat.S_ISREG(observed.st_mode):
                        raise SearchOperandError(
                            "wiki record tree contains an unsafe logical entry"
                        )
                    logical = PurePosixPath(*prefix, name)
                    size, mtime, checksum = _regular_identity(
                        directory / name, hash_content=True
                    )
                    records.append((logical, directory / name, size, mtime, checksum))
                parent.validate()
    except (OSError, ValueError) as error:
        if isinstance(error, SearchOperandError):
            raise
        raise SearchOperandError(
            f"unsafe or unavailable link candidate operand: {error}"
        ) from error

    digest = hashlib.sha256()
    digest.update(canonical(("excluded_page_path", excluded.as_posix())))
    operands = []
    for logical, operand, size, mtime, checksum in sorted(records, key=lambda item: item[0].as_posix()):
        digest.update(canonical((logical.as_posix(), size, mtime, checksum)))
        if retain_operands and logical != excluded:
            operands.append(operand)
    return revision, digest.hexdigest(), tuple(operands), (), excluded


def _stream_rg(argv: list[str], *, delimiter: bytes) -> Iterator[bytes]:
    """Drain one child completely, with bounded framing and disk-backed stderr."""
    with tempfile.TemporaryFile(mode="w+b") as errors:
        try:
            process = subprocess.Popen(
                argv, shell=False, stdout=subprocess.PIPE, stderr=errors
            )
        except FileNotFoundError as error:
            raise RgUnavailable("ripgrep (rg) is unavailable") from error
        except OSError as error:
            raise SearchExecutionError(str(error)) from error
        waited = False
        try:
            assert process.stdout is not None
            pending = b""
            while chunk := process.stdout.read(8192):
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                pending += chunk
                while delimiter in pending:
                    token, pending = pending.split(delimiter, 1)
                    if len(token) > MAX_RG_EVENT_BYTES:
                        raise SearchOutputLimitError("rg event exceeds 65,536 bytes")
                    yield token
                if len(pending) > MAX_RG_EVENT_BYTES:
                    raise SearchOutputLimitError("rg event exceeds 65,536 bytes")
            if pending:
                raise SearchExecutionError("rg emitted an unterminated record")
            code = process.wait()
            waited = True
            if code not in (0, 1):
                errors.seek(0)
                message = errors.read(16384).decode("utf-8", errors="replace")
                raise SearchExecutionError(f"rg exited with status {code}: {message}")
        finally:
            if not waited:
                process.terminate()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def filename_hits(argv: list[str], operands: tuple[Path, ...]) -> tuple[Path, ...]:
    allowed = {str(path): path for path in operands}
    hits = set()
    stream = _stream_rg(argv, delimiter=b"\0")
    try:
        for token in stream:
            try:
                name = token.decode("utf-8")
                if name not in allowed:
                    raise ValueError("rg filename is not a bound operand")
            except (UnicodeError, ValueError) as error:
                raise SearchExecutionError(str(error)) from error
            hits.add(allowed[name])
    finally:
        stream.close()
    return tuple(sorted(hits, key=str))


def _rg_text(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("rg text must be an object")
    if set(value) == {"text"} and isinstance(value["text"], str):
        value["text"].encode("utf-8")
        return value["text"]
    if set(value) == {"bytes"} and isinstance(value["bytes"], str):
        raw = base64.b64decode(value["bytes"], validate=True)
        if base64.b64encode(raw).decode("ascii") != value["bytes"]:
            raise ValueError("noncanonical base64 rg value")
        return raw.decode("utf-8")
    raise ValueError("rg text must contain exactly text or bytes")


def _rg_bytes(value: object) -> bytes:
    """Decode one canonical ripgrep text-or-bytes field without losing bytes."""

    if not isinstance(value, dict):
        raise ValueError("rg text must be an object")
    if set(value) == {"text"} and isinstance(value["text"], str):
        return value["text"].encode("utf-8")
    if set(value) == {"bytes"} and isinstance(value["bytes"], str):
        raw = base64.b64decode(value["bytes"], validate=True)
        if base64.b64encode(raw).decode("ascii") != value["bytes"]:
            raise ValueError("noncanonical base64 rg value")
        return raw
    raise ValueError("rg text must contain exactly text or bytes")


def context_hits(
    argv: list[str], operands: tuple[Path, ...], root: Path
) -> Iterator[SearchMatch]:
    allowed = {
        str(path): PurePosixPath(path.relative_to(root).as_posix()) for path in operands
    }
    stream = _stream_rg(argv, delimiter=b"\n")
    try:
        for line in stream:
            try:
                event = json.loads(line.decode("utf-8"))
                kind, data = event["type"], event["data"]
                if not isinstance(data, dict):
                    raise ValueError("rg event data must be an object")
                if kind in {"begin", "end"}:
                    if _rg_text(data["path"]) not in allowed:
                        raise ValueError("rg event path is not a candidate")
                    if kind == "end" and data.get("binary_offset") is not None:
                        raise ValueError(
                            "rg skipped binary source content; coverage is incomplete"
                        )
                    continue
                if kind == "summary":
                    if not isinstance(data.get("stats"), dict) or not isinstance(
                        data.get("elapsed_total"), dict
                    ):
                        raise ValueError("malformed rg summary")
                    continue
                if kind not in {"match", "context"}:
                    raise ValueError("unknown rg event type")
                name = _rg_text(data["path"])
                if name not in allowed:
                    raise ValueError("rg result path is not a candidate")
                number = data["line_number"]
                if type(number) is not int or number < 1:
                    raise ValueError("rg line number must be positive")
                yield SearchMatch(allowed[name], number, _rg_text(data["lines"]), kind)
            except (
                KeyError,
                TypeError,
                ValueError,
                UnicodeError,
                RecursionError,
            ) as error:
                raise SearchExecutionError(f"malformed rg event: {error}") from error
    finally:
        stream.close()


def link_candidate_hits(
    argv: list[str],
    operands: tuple[Path, ...],
    root: Path,
    terms: tuple[str, ...],
) -> Iterator[tuple[PurePosixPath, int, str, str, Literal["page", "question"]]]:
    """Yield each actual match event with a byte-proven first request term.

    This is intentionally separate from `context_hits`: ordinary search rows
    may include context and keep their historic payload, whereas a candidate
    row must be built from one validated `match` event only.
    """

    allowed = {
        str(path): PurePosixPath(path.relative_to(root).as_posix())
        for path in operands
    }
    encoded_terms = {term.encode("utf-8"): index for index, term in enumerate(terms)}
    stream = _stream_rg(argv, delimiter=b"\n")
    try:
        for line in stream:
            try:
                event = json.loads(line.decode("utf-8"))
                kind, data = event["type"], event["data"]
                if not isinstance(data, dict):
                    raise ValueError("rg event data must be an object")
                if kind in {"begin", "end"}:
                    if _rg_text(data["path"]) not in allowed:
                        raise ValueError("rg event path is not a candidate")
                    if kind == "end" and data.get("binary_offset") is not None:
                        raise ValueError(
                            "rg skipped binary source content; coverage is incomplete"
                        )
                    continue
                if kind == "summary":
                    if not isinstance(data.get("stats"), dict) or not isinstance(
                        data.get("elapsed_total"), dict
                    ):
                        raise ValueError("malformed rg summary")
                    continue
                if kind not in {"match", "context"}:
                    raise ValueError("unknown rg event type")
                name = _rg_text(data["path"])
                if name not in allowed:
                    raise ValueError("rg result path is not a candidate")
                number = data["line_number"]
                if type(number) is not int or number < 1:
                    raise ValueError("rg line number must be positive")
                raw_line = _rg_bytes(data["lines"])
                context = raw_line.decode("utf-8")
                if kind == "context":
                    continue
                submatches = data["submatches"]
                if not isinstance(submatches, list) or not submatches:
                    raise ValueError("candidate match requires byte submatches")
                matched = []
                previous_end = 0
                for submatch in submatches:
                    if not isinstance(submatch, dict) or set(submatch) != {
                        "match",
                        "start",
                        "end",
                    }:
                        raise ValueError("candidate submatch has an invalid shape")
                    start, end = submatch["start"], submatch["end"]
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or start < previous_end
                        or end <= start
                        or end > len(raw_line)
                    ):
                        raise ValueError("candidate submatch has invalid byte bounds")
                    value = _rg_bytes(submatch["match"])
                    if value != raw_line[start:end] or value not in encoded_terms:
                        raise ValueError("candidate submatch does not prove a search term")
                    previous_end = end
                    matched.append(value)
                logical = allowed[name]
                if logical.parts[:2] == ("wiki", "pages"):
                    record_kind: Literal["page", "question"] = "page"
                elif logical.parts[:2] == ("wiki", "questions"):
                    record_kind = "question"
                else:
                    raise ValueError("candidate record is outside logical wiki roots")
                selected = min(matched, key=lambda value: encoded_terms[value])
                yield logical, number, terms[encoded_terms[selected]], context, record_kind
            except (
                KeyError,
                TypeError,
                ValueError,
                UnicodeError,
                RecursionError,
            ) as error:
                raise SearchExecutionError(
                    f"malformed candidate rg event: {error}"
                ) from error
    finally:
        stream.close()


def search_active_sources(
    paths: RepoPaths,
    ledger: LedgerStore,
    request: SearchRequest,
    *,
    now: datetime | None = None,
) -> SearchResult:
    if request.scope != "sources":
        raise ValueError("source search requires sources scope")
    from .search_runs import start_search

    return start_search(paths, ledger, request, now=now)


def search_wiki(
    paths: RepoPaths,
    ledger: LedgerStore,
    request: SearchRequest,
    *,
    now: datetime | None = None,
) -> SearchResult:
    if request.scope != "wiki":
        raise ValueError("wiki search requires wiki scope")
    from .search_runs import start_search

    return start_search(paths, ledger, request, now=now)


def resume_search(
    paths: RepoPaths, ledger: LedgerStore, cursor: str, *, now: datetime | None = None
) -> SearchResult:
    from .search_runs import resume_search as resume

    return resume(paths, ledger, cursor, now=now)


def complete_search_run(pages: Sequence[SearchResult]) -> SearchRunProof:
    """Bind every page payload; retained-state verification is still required."""
    if not pages or not pages[0].terms:
        raise ValueError("complete search needs every page and nonempty terms")
    first = pages[0]
    SearchRequest(
        first.scope,
        first.pass_name,
        first.terms,
        0,
        freshness_source_ids=first.searched_source_ids
        if first.mode == "freshness"
        else (),
    )
    if first.mode not in {"research", "freshness"}:
        raise ValueError("invalid search mode")
    immutable = (
        "run_id",
        "corpus_revision",
        "scope",
        "mode",
        "pass_name",
        "terms",
        "candidate_count",
        "candidate_manifest",
        "candidate_manifest_sha256",
        "searched_source_ids",
    )
    prior = None
    offset = 0
    page_index_digest = hashlib.sha256()
    for index, page in enumerate(pages):
        final = index == len(pages) - 1
        if (
            page.page_index != index
            or page.request_cursor != prior
            or page.coverage_gaps
        ):
            raise ValueError(
                "complete search requires contiguous unblocked pages and cursors"
            )
        if any(getattr(page, field) != getattr(first, field) for field in immutable):
            raise ValueError("search page bindings disagree")
        if page.complete != final or (page.next_cursor is None) != final:
            raise ValueError(
                "complete search requires only the final page to be complete"
            )
        start = offset
        page_digest = hashlib.sha256()
        for match in page.matches:
            payload = canonical(match_record(match))
            page_digest.update(payload)
            offset += len(payload)
        checksum = page_digest.hexdigest()
        if checksum != page.result_sha256:
            raise ValueError("search page checksum does not match its payload")
        page_index_digest.update(
            canonical(
                _page_index_record(index, start, offset, len(page.matches), checksum)
            )
        )
        prior = page.next_cursor
    return SearchRunProof(
        first.run_id,
        first.corpus_revision,
        first.scope,
        first.mode,
        first.pass_name,
        first.terms,
        first.candidate_count,
        first.candidate_manifest_sha256,
        len(pages),
        sum(len(page.matches) for page in pages),
        page_index_digest.hexdigest(),
    )


def verify_search_run_proof(
    paths: RepoPaths,
    ledger: LedgerStore,
    proof: SearchRunProof,
    *,
    now: datetime | None = None,
) -> None:
    from .search_runs import verify_search_run_proof as verify

    verify(paths, ledger, proof, now=now)


def completed_search_pass(pages: Sequence[SearchResult]) -> SearchPassRecord:
    proof = complete_search_run(pages)
    if proof.scope != "sources" or proof.mode != "research" or proof.pass_name is None:
        raise ValueError("only a named source research run is a search pass")
    return SearchPassRecord(
        proof.pass_name,
        proof.terms,
        proof.run_id,
        proof.corpus_revision,
        proof.candidate_count,
        proof.candidate_manifest_sha256,
        tuple(
            SearchPageRecord(page.page_index, len(page.matches), page.result_sha256)
            for page in pages
        ),
        True,
    )


def cleanup_search_runs(
    paths: RepoPaths,
    *,
    now: datetime | None = None,
    retention: timedelta = SEARCH_RUN_TTL,
) -> tuple[PurePosixPath, ...]:
    from .search_runs import cleanup_search_runs as cleanup

    return cleanup(paths, now=now, retention=retention)
