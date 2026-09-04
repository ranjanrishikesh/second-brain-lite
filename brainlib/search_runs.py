"""Private, authenticated search spools; all access is under repository locks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal, Mapping

from .diagnostics import Diagnostic
from .layout import RepoPaths
from .ledger import LedgerStore, _PinnedDirectory, _fsync_directory
from .locking import SourceWriteLock
from .search import (
    MAX_RG_PATHS,
    SEARCH_RUN_TTL,
    InvalidSearchCursor,
    SearchError,
    SearchExecutionError,
    SearchMatch,
    SearchOperandError,
    SearchRequest,
    SearchResult,
    SearchRunBlocked,
    SearchRunExpired,
    SearchRunProof,
    SearchRunStale,
    _page_index_record,
    batch_paths,
    bind_link_candidate_operands,
    bind_operands,
    build_rg_argv,
    canonical_wiki_logical_path,
    canonical,
    context_hits,
    filename_hits,
    is_canonical_wiki_record_name,
    link_candidate_hits,
    match_record,
)

_RUN_ID = re.compile(r"srch_[0-9a-f]{32}")
_FILES = frozenset(
    {"metadata.json", "secret", "candidates.jsonl", "results.jsonl", "page-index.jsonl"}
)
_MANIFESTS = ("candidates.jsonl", "results.jsonl", "page-index.jsonl")


@dataclass(frozen=True)
class _LinkCandidate:
    path: PurePosixPath
    line: int
    matched_term: str
    context: str
    kind: Literal["page", "question"]


@dataclass(frozen=True)
class _LinkCandidatePage:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    page_index: int
    request_cursor: str | None
    next_cursor: str | None
    complete: bool
    candidate_count: int
    candidate_manifest_sha256: str
    result_sha256: str
    candidates: tuple[_LinkCandidate, ...]
    coverage_gaps: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class _LinkCandidateRunProof:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    candidate_manifest_sha256: str
    page_count: int
    candidate_count: int


def _now(now: datetime | None) -> datetime:
    result = datetime.now(timezone.utc) if now is None else now
    if (
        not isinstance(result, datetime)
        or result.tzinfo is None
        or result.utcoffset() != timedelta(0)
    ):
        raise ValueError("search time must be an aware UTC datetime")
    return result


def _repository_identity(paths: RepoPaths) -> list:
    observed = paths.root.stat()
    return [str(paths.root), observed.st_dev, observed.st_ino]


@contextmanager
def _locks(paths: RepoPaths, scope: str):
    with SourceWriteLock.acquire(paths.lock):
        with _wiki_lock(paths, scope):
            yield


def _wiki_lock(paths: RepoPaths, scope: str):
    return (
        SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock")
        if scope == "wiki"
        else nullcontext()
    )


def _run_purpose(paths: RepoPaths, metadata: dict) -> Literal["search", "link_candidates"]:
    """Validate the authenticated purpose extension with a legacy default."""

    purpose = metadata.get("purpose", "search")
    excluded = metadata.get("excluded_page_path")
    if purpose == "search" and excluded is None:
        return "search"
    if purpose == "link_candidates" and isinstance(excluded, str):
        canonical = canonical_wiki_logical_path(paths, excluded, allow_absent=True)
        if canonical.as_posix() == excluded:
            return "link_candidates"
    raise ValueError("search run purpose metadata is invalid")


class _SpoolLimit(Exception):
    pass


class _Run:
    def __init__(self, paths: RepoPaths, run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise InvalidSearchCursor("invalid search run ID")
        self.paths = paths
        self.run_id = run_id
        self.directory = paths.root / ".brain/search-runs" / run_id
        self.pinned = _PinnedDirectory.open(self.directory)
        self.meta: dict = {}
        try:
            with self.open("secret") as stream:
                self.secret = stream.read(33)
            if len(self.secret) != 32:
                raise ValueError("invalid search run secret")
        except BaseException:
            self.pinned.close()
            raise

    @classmethod
    def create(
        cls,
        paths: RepoPaths,
        request: SearchRequest,
        binding: tuple,
        now: datetime,
        *,
        purpose: Literal["search", "link_candidates"] = "search",
        excluded_page_path: PurePosixPath | None = None,
    ) -> _Run:
        if purpose == "search" and excluded_page_path is not None:
            raise ValueError("normal search runs cannot exclude a wiki page")
        if purpose == "link_candidates" and excluded_page_path is None:
            raise ValueError("candidate runs require an excluded wiki page")
        with _PinnedDirectory.open(paths.root / ".brain") as brain:
            try:
                os.mkdir("search-runs", mode=0o700, dir_fd=brain.descriptor)
            except FileExistsError:
                pass
            root_fd = brain.open_child(brain.descriptor, "search-runs")
            run_id = "srch_" + secrets.token_hex(16)
            os.mkdir(run_id, mode=0o700, dir_fd=root_fd)
            fd = brain.open_child(root_fd, run_id)
            for name in sorted(_FILES):
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=fd,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    if name == "secret":
                        stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
            _fsync_directory(fd)
            _fsync_directory(root_fd)
            brain.validate()
        run = cls(paths, run_id)
        revision, identity, _, source_ids = binding[:4]
        wiki_content_sha256s = (
            binding[4]
            if purpose == "search" and request.scope == "wiki"
            else ()
        )
        run.meta = {
            "version": 1,
            "run_id": run_id,
            "repository": _repository_identity(paths),
            "created_at": now.isoformat(),
            "expires_at": (now + SEARCH_RUN_TTL).isoformat(),
            "corpus_revision": revision,
            "operand_identity": identity,
            "scope": request.scope,
            "mode": "freshness" if request.freshness_source_ids else "research",
            "pass_name": request.pass_name,
            "terms": list(request.terms),
            "context_lines": request.context_lines,
            "page_size": request.page_size,
            "max_run_bytes": request.max_run_bytes,
            "searched_source_ids": list(source_ids),
            "freshness_source_ids": list(request.freshness_source_ids),
            "wiki_operand_content_sha256s": [
                [path.as_posix(), checksum]
                for path, checksum in wiki_content_sha256s
            ],
            "purpose": purpose,
            "excluded_page_path": (
                None if excluded_page_path is None else excluded_page_path.as_posix()
            ),
            "candidate_count": 0,
            "match_count": 0,
            "page_count": 0,
            "manifest_hashes": {},
            "highest_page_served": -1,
            "blocked": {
                "code": "search_run_incomplete",
                "message": "Search construction did not finish.",
            },
        }
        run.save()
        return run

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.pinned.close()

    def open(self, name: str, *, append: bool = False):
        if name not in _FILES:
            raise ValueError("unknown search run file")
        self.pinned.validate()
        descriptor = os.open(
            name,
            (os.O_WRONLY | os.O_APPEND if append else os.O_RDONLY)
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            dir_fd=self.pinned.descriptor,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_mode & 0o777 != 0o600
            or observed.st_nlink != 1
        ):
            os.close(descriptor)
            raise ValueError("search state must be a private regular unlinked file")
        return os.fdopen(descriptor, "ab" if append else "rb")

    def save(self) -> None:
        payload = canonical(self.meta)
        document = canonical(
            {
                "metadata": self.meta,
                "mac": hmac.new(self.secret, payload, "sha256").hexdigest(),
            }
        )
        temporary = ".metadata-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.pinned.descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            self.pinned.validate()
            os.replace(
                temporary,
                "metadata.json",
                src_dir_fd=self.pinned.descriptor,
                dst_dir_fd=self.pinned.descriptor,
            )
            _fsync_directory(self.pinned.descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.pinned.descriptor)
            except FileNotFoundError:
                pass

    def load(self) -> None:
        try:
            with self.open("metadata.json") as stream:
                payload = stream.read(16 * 1024 * 1024 + 1)
            document = json.loads(payload)
            metadata = document["metadata"]
            if (
                canonical(document) != payload
                or set(document) != {"metadata", "mac"}
                or not hmac.compare_digest(
                    document["mac"],
                    hmac.new(self.secret, canonical(metadata), "sha256").hexdigest(),
                )
            ):
                raise ValueError("search metadata authentication failed")
            self.meta = metadata
            if metadata["run_id"] != self.run_id or metadata["version"] != 1:
                raise ValueError("search metadata identity changed")
            _run_purpose(self.paths, metadata)
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            SearchOperandError,
        ) as error:
            raise SearchRunStale("retained search metadata changed") from error

    def hashes(self) -> dict[str, str]:
        hashes = {}
        for name in _MANIFESTS:
            digest = hashlib.sha256()
            with self.open(name) as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            hashes[name] = digest.hexdigest()
        return hashes

    def block(self, error: SearchRunBlocked) -> None:
        self.meta["blocked"] = {
            "code": error.diagnostic.code,
            "message": error.diagnostic.message,
        }
        self.save()

    def finish(self, *, blocked: Diagnostic | None = None) -> None:
        self.meta["manifest_hashes"] = self.hashes()
        self.meta["blocked"] = (
            None
            if blocked is None
            else {"code": blocked.code, "message": blocked.message}
        )
        self.save()

    def request(self) -> SearchRequest:
        m = self.meta
        return SearchRequest(
            m["scope"],
            m["pass_name"],
            tuple(m["terms"]),
            m["context_lines"],
            m["page_size"],
            m["max_run_bytes"],
            tuple(m["freshness_source_ids"]),
        )

    def validate_current(self, ledger: LedgerStore, now: datetime) -> None:
        if now >= datetime.fromisoformat(self.meta["expires_at"]):
            error = SearchRunExpired("search run expired; start a new search")
            if self.meta["highest_page_served"] < self.meta["page_count"] - 1:
                self.meta.setdefault("retain_until", (now + SEARCH_RUN_TTL).isoformat())
            self.block(error)
            raise error
        blocked = self.meta["blocked"]
        if blocked:
            cls = {
                "search_run_stale": SearchRunStale,
                "search_run_expired": SearchRunExpired,
            }.get(blocked["code"], SearchRunBlocked)
            raise cls(
                blocked["message"],
                diagnostic=Diagnostic(blocked["code"], blocked["message"]),
            )
        try:
            purpose = _run_purpose(self.paths, self.meta)
            if purpose == "link_candidates":
                revision, identity, _, source_ids, excluded = bind_link_candidate_operands(
                    self.paths,
                    ledger,
                    self.meta["excluded_page_path"],
                    retain_operands=False,
                )
                if excluded.as_posix() != self.meta["excluded_page_path"]:
                    raise ValueError("candidate excluded path changed")
            else:
                revision, identity, _, source_ids, wiki_content_sha256s = bind_operands(
                    self.paths, ledger, self.request(), retain_operands=False
                )
                if self.meta["scope"] == "wiki" and _wiki_operand_content_sha256s(
                    self.meta
                ) != wiki_content_sha256s:
                    raise ValueError("search wiki operand content changed")
            if (
                self.meta["repository"] != _repository_identity(self.paths)
                or revision != self.meta["corpus_revision"]
                or identity != self.meta["operand_identity"]
                or list(source_ids) != self.meta["searched_source_ids"]
            ):
                raise ValueError("search corpus or operand identity changed")
            if self.hashes() != self.meta["manifest_hashes"]:
                raise ValueError("retained search manifest changed")
            self.pinned.validate()
        except (SearchOperandError, OSError, ValueError) as cause:
            error = SearchRunStale(str(cause))
            self.block(error)
            raise error from cause

    def cursor(self, index: int) -> str:
        payload = {
            "version": 1,
            "run_id": self.run_id,
            "page_index": index,
            "expires_at": self.meta["expires_at"],
        }
        payload["mac"] = hmac.new(self.secret, canonical(payload), "sha256").hexdigest()
        return _encode_cursor(payload)

    def page(self, index: int, cursor: str | None) -> SearchResult:
        if _run_purpose(self.paths, self.meta) != "search":
            raise InvalidSearchCursor("candidate run cannot be resumed as normal search")
        if self.meta["blocked"]:
            diagnostic = Diagnostic(**self.meta["blocked"])
            return self.result(
                index, cursor, (), hashlib.sha256(b"").hexdigest(), gaps=(diagnostic,)
            )
        entry = None
        with self.open("page-index.jsonl") as stream:
            for line in stream:
                value = json.loads(line)
                if value["page_index"] == index:
                    entry = value
                    break
        if entry is None:
            raise InvalidSearchCursor("search page is outside the retained run")
        with self.open("results.jsonl") as stream:
            stream.seek(entry["start"])
            payload = stream.read(entry["end"] - entry["start"])
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            error = SearchRunStale("retained page checksum changed")
            self.block(error)
            raise error
        records = [json.loads(line) for line in payload.splitlines()]
        if (
            len(records) != entry["record_count"]
            or len(records) > self.meta["page_size"]
        ):
            error = SearchRunStale("retained page count changed")
            self.block(error)
            raise error
        matches = tuple(
            SearchMatch(
                PurePosixPath(item["path"]),
                item["line_number"],
                item["text"],
                item["kind"],
            )
            for item in records
        )
        # Serving a later page without its predecessor is forbidden, even with
        # a locally reconstructed valid MAC. Replaying an already served page is safe.
        if index > self.meta["highest_page_served"] + 1:
            raise InvalidSearchCursor("search pages must be served in order")
        self.meta["highest_page_served"] = max(index, self.meta["highest_page_served"])
        self.save()
        return self.result(index, cursor, matches, entry["sha256"])

    def result(
        self,
        index: int,
        cursor: str | None,
        matches: tuple,
        checksum: str,
        *,
        gaps: tuple = (),
    ) -> SearchResult:
        m = self.meta
        complete = not gaps and index == m["page_count"] - 1
        return SearchResult(
            self.run_id,
            m["corpus_revision"],
            m["scope"],
            m["mode"],
            m["pass_name"],
            tuple(m["terms"]),
            index,
            cursor,
            None if complete or gaps else self.cursor(index + 1),
            complete,
            m["candidate_count"],
            PurePosixPath(".brain/search-runs", self.run_id, "candidates.jsonl"),
            m["manifest_hashes"].get(
                "candidates.jsonl", hashlib.sha256(b"").hexdigest()
            ),
            checksum,
            matches,
            tuple(m["searched_source_ids"]),
            gaps,
        )


class _Spool:
    def __init__(self, run: _Run) -> None:
        self.run = run
        self.used = 0
        self.offset = 0
        self.page_start = 0
        self.page_records = 0
        self.page_digest = hashlib.sha256()

    def append(self, stream, payload: bytes) -> None:
        if self.used + len(payload) > self.run.meta["max_run_bytes"]:
            raise _SpoolLimit
        stream.write(payload)
        self.used += len(payload)

    def index(self, stream) -> None:
        record = _page_index_record(
            self.run.meta["page_count"],
            self.page_start,
            self.offset,
            self.page_records,
            self.page_digest.hexdigest(),
        )
        self.append(stream, canonical(record))
        self.run.meta["page_count"] += 1
        self.page_start = self.offset
        self.page_records = 0
        self.page_digest = hashlib.sha256()


def _candidate_batches(run: _Run, fixed: tuple[str, ...]) -> Iterator[tuple[Path, ...]]:
    with run.open("candidates.jsonl") as stream:
        pending = []
        for line in stream:
            pending.append(run.paths.root / json.loads(line)["path"])
            if len(pending) == MAX_RG_PATHS:
                yield from batch_paths(tuple(pending), fixed_argv=fixed)
                pending.clear()
        if pending:
            yield from batch_paths(tuple(pending), fixed_argv=fixed)


def _build(run: _Run, request: SearchRequest, operands: tuple[Path, ...]) -> None:
    spool = _Spool(run)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix="brain-search-pattern-"
    ) as pattern:
        for term in request.terms:
            pattern.write(term.encode("utf-8") + b"\n")
        pattern.flush()
        filename_argv = build_rg_argv(
            request, phase="filenames", pattern_file=Path(pattern.name)
        )
        context_argv = build_rg_argv(
            request, phase="context", pattern_file=Path(pattern.name)
        )
        with run.open("candidates.jsonl", append=True) as candidates:
            for batch in batch_paths(operands, fixed_argv=tuple(filename_argv)):
                for path in filename_hits(
                    filename_argv + [str(item) for item in batch], batch
                ):
                    spool.append(
                        candidates,
                        canonical(
                            {"path": path.relative_to(run.paths.root).as_posix()}
                        ),
                    )
                    run.meta["candidate_count"] += 1
            candidates.flush()
            os.fsync(candidates.fileno())
        with (
            run.open("results.jsonl", append=True) as results,
            run.open("page-index.jsonl", append=True) as index,
        ):
            for batch in _candidate_batches(run, tuple(context_argv)):
                stream = context_hits(
                    context_argv + [str(item) for item in batch], batch, run.paths.root
                )
                try:
                    for match in stream:
                        payload = canonical(match_record(match))
                        spool.append(results, payload)
                        spool.offset += len(payload)
                        spool.page_records += 1
                        spool.page_digest.update(payload)
                        run.meta["match_count"] += 1
                        if spool.page_records == request.page_size:
                            spool.index(index)
                finally:
                    stream.close()
            if spool.page_records or not run.meta["page_count"]:
                spool.index(index)
            for stream in (results, index):
                stream.flush()
                os.fsync(stream.fileno())


def _candidate_record(candidate: _LinkCandidate) -> dict[str, object]:
    path = candidate.path
    if (
        type(path) is not PurePosixPath
        or len(path.parts) != 3
        or path.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")}
        or not is_canonical_wiki_record_name(path.name)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("candidate path is not a direct logical wiki record")
    expected_kind = "page" if path.parts[1] == "pages" else "question"
    if (
        type(candidate.line) is not int
        or candidate.line < 1
        or not isinstance(candidate.matched_term, str)
        or not candidate.matched_term
        or not isinstance(candidate.context, str)
        or candidate.kind != expected_kind
    ):
        raise ValueError("candidate record is invalid")
    candidate.matched_term.encode("utf-8")
    candidate.context.encode("utf-8")
    return {
        "path": path.as_posix(),
        "line": candidate.line,
        "matched_term": candidate.matched_term,
        "context": candidate.context,
        "kind": candidate.kind,
    }


def _decode_candidate_record(value: object, terms: tuple[str, ...]) -> _LinkCandidate:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "line",
        "matched_term",
        "context",
        "kind",
    }:
        raise ValueError("candidate record has an invalid shape")
    path_text = value["path"]
    if not isinstance(path_text, str) or "\\" in path_text or "\0" in path_text:
        raise ValueError("candidate record path is invalid")
    path = PurePosixPath(path_text)
    candidate = _LinkCandidate(
        path,
        value["line"],
        value["matched_term"],
        value["context"],
        value["kind"],
    )
    if candidate.matched_term not in terms:
        raise ValueError("candidate record term is not bound to the run")
    _candidate_record(candidate)
    if path.as_posix() != path_text:
        raise ValueError("candidate record path is not canonical")
    return candidate


def _decode_candidate_page_index(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "page_index",
        "start",
        "end",
        "record_count",
        "sha256",
    }:
        raise ValueError("candidate page index has an invalid shape")
    for field in ("page_index", "start", "end", "record_count"):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError("candidate page index has invalid offsets")
    if value["end"] < value["start"]:
        raise ValueError("candidate page index has invalid offsets")
    if not isinstance(value["sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["sha256"]
    ):
        raise ValueError("candidate page index has invalid checksum")
    return value


def _candidate_page(run: _Run, index: int, cursor: str | None) -> _LinkCandidatePage:
    if _run_purpose(run.paths, run.meta) != "link_candidates":
        raise InvalidSearchCursor("normal run cannot be resumed as link candidates")
    if run.meta["blocked"]:
        diagnostic = Diagnostic(**run.meta["blocked"])
        return _candidate_page_result(
            run,
            index,
            cursor,
            (),
            hashlib.sha256(b"").hexdigest(),
            gaps=(diagnostic,),
        )
    entries: list[dict[str, object]] = []
    with run.open("page-index.jsonl") as stream:
        for expected_index, raw in enumerate(stream):
            value = _decode_candidate_page_index(json.loads(raw))
            if canonical(value) != raw or value["page_index"] != expected_index:
                raise SearchRunStale("retained candidate page index changed")
            if entries and value["start"] != entries[-1]["end"]:
                raise SearchRunStale("retained candidate page ranges changed")
            entries.append(value)
    if len(entries) != run.meta["page_count"] or index >= len(entries):
        raise InvalidSearchCursor("candidate page is outside the retained run")
    entry = entries[index]
    with run.open("results.jsonl") as stream:
        stream.seek(entry["start"])
        payload = stream.read(entry["end"] - entry["start"])
    if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
        error = SearchRunStale("retained candidate page checksum changed")
        run.block(error)
        raise error
    records = []
    for raw in payload.splitlines(keepends=True):
        value = json.loads(raw)
        if canonical(value) != raw:
            error = SearchRunStale("retained candidate record is not canonical")
            run.block(error)
            raise error
        records.append(_decode_candidate_record(value, tuple(run.meta["terms"])))
    if (
        len(records) != entry["record_count"]
        or len(records) > run.meta["page_size"]
    ):
        error = SearchRunStale("retained candidate page count changed")
        run.block(error)
        raise error
    if index > run.meta["highest_page_served"] + 1:
        raise InvalidSearchCursor("candidate pages must be served in order")
    run.meta["highest_page_served"] = max(index, run.meta["highest_page_served"])
    run.save()
    return _candidate_page_result(run, index, cursor, tuple(records), entry["sha256"])


def _candidate_page_result(
    run: _Run,
    index: int,
    cursor: str | None,
    candidates: tuple[_LinkCandidate, ...],
    checksum: str,
    *,
    gaps: tuple[Diagnostic, ...] = (),
) -> _LinkCandidatePage:
    metadata = run.meta
    complete = not gaps and index == metadata["page_count"] - 1
    return _LinkCandidatePage(
        run.run_id,
        metadata["corpus_revision"],
        PurePosixPath(metadata["excluded_page_path"]),
        tuple(metadata["terms"]),
        index,
        cursor,
        None if complete or gaps else run.cursor(index + 1),
        complete,
        metadata["candidate_count"],
        metadata["manifest_hashes"].get(
            "candidates.jsonl", hashlib.sha256(b"").hexdigest()
        ),
        checksum,
        candidates,
        gaps,
    )


def _build_link_candidates(
    run: _Run, request: SearchRequest, operands: tuple[Path, ...]
) -> None:
    """Spool exactly the first actual match record from each other wiki file."""

    spool = _Spool(run)
    previous_path: str | None = None
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix="brain-link-candidate-pattern-"
    ) as pattern:
        for term in request.terms:
            pattern.write(term.encode("utf-8") + b"\n")
        pattern.flush()
        context_argv = build_rg_argv(
            request, phase="context", pattern_file=Path(pattern.name)
        )
        with (
            run.open("candidates.jsonl", append=True) as candidates,
            run.open("results.jsonl", append=True) as results,
            run.open("page-index.jsonl", append=True) as index,
        ):
            for batch in batch_paths(operands, fixed_argv=tuple(context_argv)):
                emitted: set[PurePosixPath] = set()
                stream = link_candidate_hits(
                    context_argv + [str(item) for item in batch],
                    batch,
                    run.paths.root,
                    request.terms,
                )
                try:
                    for path, line, term, context, kind in stream:
                        key = path.as_posix()
                        if previous_path is not None and key < previous_path:
                            raise SearchExecutionError(
                                "rg candidate paths are not in canonical order"
                            )
                        previous_path = key
                        if path in emitted:
                            continue
                        emitted.add(path)
                        candidate = _LinkCandidate(path, line, term, context, kind)
                        payload = canonical(_candidate_record(candidate))
                        spool.append(candidates, payload)
                        spool.append(results, payload)
                        spool.offset += len(payload)
                        spool.page_records += 1
                        spool.page_digest.update(payload)
                        run.meta["candidate_count"] += 1
                        run.meta["match_count"] += 1
                        if spool.page_records == request.page_size:
                            spool.index(index)
                finally:
                    stream.close()
            if spool.page_records or not run.meta["page_count"]:
                spool.index(index)
            for stream in (candidates, results, index):
                stream.flush()
                os.fsync(stream.fileno())


def start_search(
    paths: RepoPaths,
    ledger: LedgerStore,
    request: SearchRequest,
    *,
    now: datetime | None = None,
) -> SearchResult:
    current = _now(now)
    with _locks(paths, request.scope):
        binding = bind_operands(paths, ledger, request)
        with _Run.create(paths, request, binding, current) as run:
            try:
                _build(run, request, binding[2])
                # Discovery no longer needs its full operand list after spooling.
                # Revalidation streams identities without retaining a second list.
                binding = None
                run.finish()
                run.validate_current(ledger, current)
            except _SpoolLimit:
                run.finish(
                    blocked=Diagnostic(
                        "search_spool_limit",
                        "Search spool byte limit exceeded; coverage is incomplete.",
                    )
                )
            except SearchError as error:
                run.finish(
                    blocked=getattr(
                        error, "diagnostic", Diagnostic(error.code, str(error))
                    )
                )
                raise
            except (OSError, ValueError) as error:
                diagnostic = Diagnostic(
                    "search_run_incomplete", f"Search construction failed: {error}"
                )
                run.finish(blocked=diagnostic)
                raise SearchRunBlocked(
                    diagnostic.message, diagnostic=diagnostic
                ) from error
            return run.page(0, None)


def _start_link_candidate_run(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    page_path: Path | PurePosixPath | str,
    terms: tuple[str, ...],
    page_size: int = 100,
    max_run_bytes: int = 1_073_741_824,
    now: datetime | None = None,
) -> _LinkCandidatePage:
    """Start the private retained run consumed by graph's public adapter."""

    request = SearchRequest(
        "wiki", None, terms, 0, page_size=page_size, max_run_bytes=max_run_bytes
    )
    current = _now(now)
    with _locks(paths, "wiki"):
        binding = bind_link_candidate_operands(paths, ledger, page_path)
        excluded = binding[4]
        with _Run.create(
            paths,
            request,
            binding,
            current,
            purpose="link_candidates",
            excluded_page_path=excluded,
        ) as run:
            try:
                _build_link_candidates(run, request, binding[2])
                run.finish()
                run.validate_current(ledger, current)
            except _SpoolLimit:
                run.finish(
                    blocked=Diagnostic(
                        "search_spool_limit",
                        "Search spool byte limit exceeded; coverage is incomplete.",
                    )
                )
            except SearchError as error:
                run.finish(
                    blocked=getattr(
                        error, "diagnostic", Diagnostic(error.code, str(error))
                    )
                )
                raise
            except (OSError, ValueError) as error:
                diagnostic = Diagnostic(
                    "search_run_incomplete", f"Search construction failed: {error}"
                )
                run.finish(blocked=diagnostic)
                raise SearchRunBlocked(
                    diagnostic.message, diagnostic=diagnostic
                ) from error
            return _candidate_page(run, 0, None)


def _complete_link_candidate_run(
    pages: tuple[_LinkCandidatePage, ...] | list[_LinkCandidatePage],
) -> _LinkCandidateRunProof:
    """Check a caller's complete candidate page chain before making a proof."""

    if not pages:
        raise ValueError("complete candidate search needs every page")
    first = pages[0]
    if not isinstance(first, _LinkCandidatePage):
        raise ValueError("candidate page has an invalid type")
    SearchRequest("wiki", None, first.terms, 0)
    if not re.fullmatch(r"srch_[0-9a-f]{32}", first.run_id) or not re.fullmatch(
        r"[0-9a-f]{64}", first.corpus_revision
    ) or not re.fullmatch(r"[0-9a-f]{64}", first.candidate_manifest_sha256):
        raise ValueError("candidate page bindings are invalid")
    if (
        type(first.page_path) is not PurePosixPath
        or len(first.page_path.parts) != 3
        or first.page_path.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")}
    ):
        raise ValueError("candidate page path is invalid")
    immutable = (
        "run_id",
        "corpus_revision",
        "page_path",
        "terms",
        "candidate_count",
        "candidate_manifest_sha256",
    )
    prior: str | None = None
    candidate_manifest = hashlib.sha256()
    last_path: str | None = None
    for index, page in enumerate(pages):
        final = index == len(pages) - 1
        if (
            not isinstance(page, _LinkCandidatePage)
            or page.page_index != index
            or page.request_cursor != prior
            or page.coverage_gaps
        ):
            raise ValueError(
                "complete candidate search requires contiguous unblocked pages and cursors"
            )
        if any(getattr(page, field) != getattr(first, field) for field in immutable):
            raise ValueError("candidate page bindings disagree")
        if page.complete != final or (page.next_cursor is None) != final:
            raise ValueError(
                "complete candidate search requires only the final page to be complete"
            )
        if not final and (not isinstance(page.next_cursor, str) or not page.next_cursor):
            raise ValueError("candidate continuation cursor is invalid")
        checksum = hashlib.sha256()
        for candidate in page.candidates:
            payload = canonical(_candidate_record(candidate))
            checksum.update(payload)
            candidate_manifest.update(payload)
            key = candidate.path.as_posix()
            if last_path is not None and key <= last_path:
                raise ValueError("candidate records are not in canonical path order")
            last_path = key
        if checksum.hexdigest() != page.result_sha256:
            raise ValueError("candidate page checksum does not match its payload")
        prior = page.next_cursor
    if first.candidate_count != sum(len(page.candidates) for page in pages):
        raise ValueError("candidate page counts do not match the run")
    if candidate_manifest.hexdigest() != first.candidate_manifest_sha256:
        raise ValueError("candidate manifest does not match every page payload")
    return _LinkCandidateRunProof(
        first.run_id,
        first.corpus_revision,
        first.page_path,
        first.terms,
        first.candidate_manifest_sha256,
        len(pages),
        first.candidate_count,
    )


def _encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(canonical(value)).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    try:
        if (
            not isinstance(cursor, str)
            or not 1 <= len(cursor) <= 4096
            or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
        ):
            raise ValueError
        payload = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(payload)
        if _encode_cursor(value) != cursor or set(value) != {
            "version",
            "run_id",
            "page_index",
            "expires_at",
            "mac",
        }:
            raise ValueError
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not _RUN_ID.fullmatch(value["run_id"])
        ):
            raise ValueError
        if (
            type(value["page_index"]) is not int
            or value["page_index"] < 1
            or not isinstance(value["expires_at"], str)
        ):
            raise ValueError
        if not isinstance(value["mac"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", value["mac"]
        ):
            raise ValueError
        return value
    except (TypeError, ValueError, KeyError, RecursionError) as error:
        raise InvalidSearchCursor("malformed search cursor") from error


def resume_search(
    paths: RepoPaths, ledger: LedgerStore, cursor: str, *, now: datetime | None = None
) -> SearchResult:
    current = _now(now)
    value = _decode_cursor(cursor)
    with SourceWriteLock.acquire(paths.lock):
        try:
            run = _Run(paths, value["run_id"])
        except (OSError, ValueError) as error:
            raise InvalidSearchCursor("unknown or unsafe search cursor run") from error
        with run:
            mac = value.pop("mac")
            if not hmac.compare_digest(
                mac, hmac.new(run.secret, canonical(value), "sha256").hexdigest()
            ):
                raise InvalidSearchCursor("search cursor authentication failed")
            run.load()
            if _run_purpose(paths, run.meta) != "search":
                raise InvalidSearchCursor(
                    "candidate run cannot be resumed as normal search"
                )
            if (
                value["expires_at"] != run.meta["expires_at"]
                or value["page_index"] >= run.meta["page_count"]
            ):
                raise InvalidSearchCursor("search cursor is outside its run")
            with _wiki_lock(paths, run.meta["scope"]):
                run.validate_current(ledger, current)
                return run.page(value["page_index"], cursor)


def _resume_link_candidate_run(
    paths: RepoPaths,
    ledger: LedgerStore,
    cursor: str,
    *,
    now: datetime | None = None,
) -> _LinkCandidatePage:
    """Resume only a retained candidate-purpose run."""

    current = _now(now)
    value = _decode_cursor(cursor)
    with SourceWriteLock.acquire(paths.lock):
        try:
            run = _Run(paths, value["run_id"])
        except (OSError, ValueError) as error:
            raise InvalidSearchCursor("unknown or unsafe search cursor run") from error
        with run:
            mac = value.pop("mac")
            if not hmac.compare_digest(
                mac, hmac.new(run.secret, canonical(value), "sha256").hexdigest()
            ):
                raise InvalidSearchCursor("search cursor authentication failed")
            run.load()
            if _run_purpose(paths, run.meta) != "link_candidates":
                raise InvalidSearchCursor("normal run cannot be resumed as link candidates")
            if (
                value["expires_at"] != run.meta["expires_at"]
                or value["page_index"] >= run.meta["page_count"]
            ):
                raise InvalidSearchCursor("search cursor is outside its run")
            with _wiki_lock(paths, run.meta["scope"]):
                run.validate_current(ledger, current)
                return _candidate_page(run, value["page_index"], cursor)


def verify_search_run_proof(
    paths: RepoPaths,
    ledger: LedgerStore,
    proof: SearchRunProof,
    *,
    now: datetime | None = None,
) -> None:
    with SourceWriteLock.acquire(paths.lock):
        # Hold the wiki write lock even for source searches: it keeps this public
        # verifier safe when a forged proof claims the wrong scope and gives
        # callers of the locked verifier one consistent lock contract.
        with _wiki_lock(paths, "wiki"):
            _verify_search_run_proof_locked(paths, ledger, proof, now=now)


def _verify_search_run_proof_locked(
    paths: RepoPaths,
    ledger: LedgerStore,
    proof: SearchRunProof,
    *,
    now: datetime | None = None,
    captured_wiki_documents: Mapping[PurePosixPath, tuple[Path, str]] | None = None,
) -> None:
    """Verify normal retained search evidence while source then wiki locks are held."""

    current = _now(now)
    try:
        run = _Run(paths, proof.run_id)
    except (OSError, ValueError, InvalidSearchCursor) as error:
        raise SearchRunStale("search proof has no safe retained run") from error
    with run:
        run.load()
        if _run_purpose(paths, run.meta) != "search":
            raise SearchRunBlocked(
                "candidate run cannot be verified as a normal search proof"
            )
        run.validate_current(ledger, current)
        expected = {
            field: run.meta[field]
            for field in (
                "run_id",
                "corpus_revision",
                "scope",
                "mode",
                "pass_name",
                "terms",
                "candidate_count",
                "page_count",
                "match_count",
            )
        }
        expected["terms"] = tuple(expected["terms"])
        expected["candidate_manifest_sha256"] = run.meta["manifest_hashes"][
            "candidates.jsonl"
        ]
        expected["page_index_sha256"] = run.meta["manifest_hashes"][
            "page-index.jsonl"
        ]
        if (
            asdict(proof) != expected
            or run.meta["highest_page_served"] != run.meta["page_count"] - 1
        ):
            raise SearchRunBlocked(
                "Search proof is incomplete or differs from retained evidence."
            )
        if captured_wiki_documents is not None:
            _verify_captured_wiki_documents(run.meta, paths, captured_wiki_documents)


def _wiki_operand_content_sha256s(
    metadata: Mapping[str, object],
) -> tuple[tuple[PurePosixPath, str], ...]:
    """Decode the MAC-protected wiki byte manifest recorded with a normal run."""

    raw = metadata.get("wiki_operand_content_sha256s")
    if not isinstance(raw, list):
        raise ValueError("search run has no wiki operand content manifest")
    result: list[tuple[PurePosixPath, str]] = []
    for entry in raw:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or not isinstance(entry[1], str)
        ):
            raise ValueError("search wiki operand content manifest is malformed")
        logical = PurePosixPath(entry[0])
        if (
            logical.as_posix() != entry[0]
            or logical.is_absolute()
            or len(logical.parts) != 3
            or logical.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")}
            or not is_canonical_wiki_record_name(logical.name)
            or re.fullmatch(r"[0-9a-f]{64}", entry[1]) is None
        ):
            raise ValueError("search wiki operand content manifest is malformed")
        result.append((logical, entry[1]))
    ordered = tuple(sorted(result, key=lambda item: item[0].as_posix()))
    if tuple(result) != ordered or len({path for path, _checksum in result}) != len(result):
        raise ValueError("search wiki operand content manifest is noncanonical")
    return ordered


def _verify_captured_wiki_documents(
    metadata: Mapping[str, object],
    paths: RepoPaths,
    documents: Mapping[PurePosixPath, tuple[Path, str]],
) -> None:
    """Bind pinned captured bytes to the authenticated normal-run operand set."""

    try:
        expected = _wiki_operand_content_sha256s(metadata)
        observed: list[tuple[PurePosixPath, str]] = []
        for logical, value in documents.items():
            if (
                not isinstance(logical, PurePosixPath)
                or not isinstance(value, tuple)
                or len(value) != 2
                or not isinstance(value[0], Path)
                or value[0] != paths.root / logical
                or not isinstance(value[1], str)
            ):
                raise ValueError("captured wiki document mapping is malformed")
            observed.append(
                (
                    logical,
                    hashlib.sha256(value[1].encode("utf-8", errors="strict")).hexdigest(),
                )
            )
        observed_tuple = tuple(sorted(observed, key=lambda item: item[0].as_posix()))
        if expected != observed_tuple:
            raise ValueError("captured wiki bytes differ from retained search operands")
    except (TypeError, UnicodeError, ValueError) as error:
        raise SearchRunBlocked(
            "Captured wiki bytes differ from retained search evidence."
        ) from error


def _verify_link_candidate_run_locked(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    run_id: str,
    corpus_revision: str,
    page_path: PurePosixPath,
    terms: tuple[str, ...],
    candidate_manifest_sha256: str,
    page_count: int,
    candidate_count: int,
    now: datetime | None = None,
) -> None:
    """Verify candidate evidence while a transaction already owns both locks.

    This deliberately opens no `SourceWriteLock` or wiki lock. Callers must
    establish source-then-wiki ownership before invoking it.
    """

    current = _now(now)
    try:
        requested_path = canonical_wiki_logical_path(
            paths, page_path, allow_absent=True
        )
        SearchRequest("wiki", None, terms, 0)
    except (SearchOperandError, ValueError, TypeError) as error:
        raise SearchRunBlocked("candidate proof has invalid bindings") from error
    try:
        run = _Run(paths, run_id)
    except (OSError, ValueError, InvalidSearchCursor) as error:
        raise SearchRunStale("candidate proof has no safe retained run") from error
    with run:
        run.load()
        if _run_purpose(paths, run.meta) != "link_candidates":
            raise SearchRunBlocked("normal run cannot verify a candidate proof")
        run.validate_current(ledger, current)
        expected = {
            "run_id": run.meta["run_id"],
            "corpus_revision": run.meta["corpus_revision"],
            "page_path": PurePosixPath(run.meta["excluded_page_path"]),
            "terms": tuple(run.meta["terms"]),
            "candidate_manifest_sha256": run.meta["manifest_hashes"][
                "candidates.jsonl"
            ],
            "page_count": run.meta["page_count"],
            "candidate_count": run.meta["candidate_count"],
        }
        supplied = {
            "run_id": run_id,
            "corpus_revision": corpus_revision,
            "page_path": requested_path,
            "terms": terms,
            "candidate_manifest_sha256": candidate_manifest_sha256,
            "page_count": page_count,
            "candidate_count": candidate_count,
        }
        if (
            supplied != expected
            or run.meta["match_count"] != run.meta["candidate_count"]
            or run.meta["page_count"] < 1
            or run.meta["highest_page_served"] != run.meta["page_count"] - 1
        ):
            raise SearchRunBlocked(
                "Candidate proof is incomplete or differs from retained evidence."
            )


def cleanup_search_runs(
    paths: RepoPaths,
    *,
    now: datetime | None = None,
    retention: timedelta = SEARCH_RUN_TTL,
) -> tuple[PurePosixPath, ...]:
    current = _now(now)
    if not isinstance(retention, timedelta) or retention <= timedelta(0):
        raise ValueError("search retention must be positive")
    removed = []
    with SourceWriteLock.acquire(paths.lock):
        try:
            parent = _PinnedDirectory.open(paths.root / ".brain/search-runs")
        except FileNotFoundError:
            return ()
        with parent:
            for name in sorted(os.listdir(parent.descriptor)):
                if not _RUN_ID.fullmatch(name) or not stat.S_ISDIR(
                    os.stat(
                        name, dir_fd=parent.descriptor, follow_symlinks=False
                    ).st_mode
                ):
                    continue
                try:
                    with _Run(paths, name) as run:
                        run.load()
                        with _wiki_lock(paths, run.meta["scope"]):
                            expired_at = datetime.fromisoformat(run.meta["expires_at"])
                            if current < expired_at:
                                continue
                            complete = (
                                not run.meta["blocked"]
                                and run.meta["highest_page_served"]
                                == run.meta["page_count"] - 1
                            )
                            if not complete and not run.meta["blocked"]:
                                run.meta["retain_until"] = (
                                    current + retention
                                ).isoformat()
                                run.block(
                                    SearchRunExpired(
                                        "search run expired; start a new search"
                                    )
                                )
                                continue
                            horizon = (
                                datetime.fromisoformat(run.meta["retain_until"])
                                if "retain_until" in run.meta
                                else expired_at
                            )
                            if current < horizon:
                                continue
                            if set(os.listdir(run.pinned.descriptor)) != _FILES:
                                continue
                            # Open every file first: no symlink, hardlink, device,
                            # or unexpected child is ever followed or removed.
                            for filename in _FILES:
                                with run.open(filename):
                                    pass
                            run.pinned.validate()
                            parent.validate()
                            for filename in _FILES:
                                os.unlink(filename, dir_fd=run.pinned.descriptor)
                            os.rmdir(name, dir_fd=parent.descriptor)
                            _fsync_directory(parent.descriptor)
                            removed.append(PurePosixPath(".brain/search-runs", name))
                except (OSError, ValueError, SearchRunStale):
                    continue
    return tuple(removed)
