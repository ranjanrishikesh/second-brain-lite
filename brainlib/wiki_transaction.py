"""The sole durable publication path for Second Brain Lite wiki records."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from .citations import (
    canonicalize_citation_destinations,
    rewrite_historical_original_links,
    validate_citations,
)
from .contracts import compute_corpus_revision
from .diagnostics import Diagnostic, ValidationIssue, ValidationReport
from .graph import (
    LinkCandidateRunProof,
    _load_live_documents,
    render_wiki_index,
    validate_graph,
)
from .layout import RepoPaths
from .ledger import (
    CitationRewrite,
    LedgerStore,
    UnsafeFilesystemError,
    _PinnedDirectory,
    _fsync_directory,
    _read_regular_at,
)
from .locking import (
    SourceWriteLock,
    _NATIVE_RENAME_NO_REPLACE,
    _rename_no_replace_at,
)
from .markdown import scan_markdown
from .search import is_canonical_wiki_record_name
from .search_runs import _verify_link_candidate_run_locked
from .wiki_models import parse_page, parse_question


ChangeIntent = Literal[
    "routine",
    "rename",
    "delete",
    "merge",
    "split",
    "remove_claim",
    "remove_relationship",
    "resolve_contradiction",
    "major_uncertain_rewrite",
    "ambiguous_rename",
]

_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"wstg_[0-9a-f]{32}")
_SEARCH_RUN_ID = re.compile(r"srch_[0-9a-f]{32}")
_INTENTS = frozenset(
    {
        "routine",
        "rename",
        "delete",
        "merge",
        "split",
        "remove_claim",
        "remove_relationship",
        "resolve_contradiction",
        "major_uncertain_rewrite",
        "ambiguous_rename",
    }
)
_APPROVAL_INTENTS = frozenset(
    {
        "delete",
        "merge",
        "split",
        "remove_claim",
        "remove_relationship",
        "resolve_contradiction",
        "major_uncertain_rewrite",
        "ambiguous_rename",
    }
)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _require_hex(value: object, name: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def _require_posix_text(value: object, name: str) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value or "\0" in value:
        raise ValueError(f"{name} must be canonical repository-relative POSIX text")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{name} must be canonical repository-relative POSIX text")
    return path


def _require_wiki_path(value: object, name: str = "path") -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise ValueError(f"{name} must be a repository-relative POSIX path")
    path = _require_posix_text(value.as_posix(), name)
    if (
        len(path.parts) != 3
        or path.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")}
        or not is_canonical_wiki_record_name(path.name)
    ):
        raise ValueError(f"{name} must name a direct wiki/pages or wiki/questions Markdown record")
    return path


def _staging_run(path: PurePosixPath) -> str:
    if (
        len(path.parts) != 7
        or path.parts[:3] != (".brain", "wiki-staging", path.parts[2])
        or not _RUN_ID.fullmatch(path.parts[2])
        or path.parts[3:5] != ("files", "wiki")
        or path.parts[5] not in {"pages", "questions"}
        or not is_canonical_wiki_record_name(path.parts[6])
    ):
        raise ValueError("staging_path must be an exact same-run wiki staging path")
    return path.parts[2]


def _require_staging_path(value: object, target: PurePosixPath) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise ValueError("staging_path must be a repository-relative POSIX path")
    path = _require_posix_text(value.as_posix(), "staging_path")
    _staging_run(path)
    if path.parts[4:] != target.parts:
        raise ValueError("staging_path must name the exact logical target beneath files")
    return path


@dataclass(frozen=True)
class WikiChange:
    operation: Literal["write", "delete"]
    path: PurePosixPath
    staging_path: PurePosixPath | None
    sha256: str | None

    def __post_init__(self) -> None:
        _require_wiki_path(self.path)
        if self.operation == "write":
            _require_staging_path(self.staging_path, self.path)
            _require_hex(self.sha256, "sha256")
        elif self.operation == "delete":
            if self.staging_path is not None or self.sha256 is not None:
                raise ValueError("delete changes cannot carry a staging path or checksum")
        else:
            raise ValueError("operation must be write or delete")

    @classmethod
    def write(
        cls, path: PurePosixPath, staging_path: PurePosixPath, sha256: str
    ) -> "WikiChange":
        return cls("write", path, staging_path, sha256)

    @classmethod
    def delete(cls, path: PurePosixPath) -> "WikiChange":
        return cls("delete", path, None, None)

    def to_dict(self) -> dict[str, object]:
        if self.operation == "delete":
            return {"operation": "delete", "path": self.path.as_posix()}
        assert self.staging_path is not None and self.sha256 is not None
        return {
            "operation": "write",
            "path": self.path.as_posix(),
            "staging_path": self.staging_path.as_posix(),
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "WikiChange":
        if not isinstance(value, dict):
            raise ValueError("manifest change must be an object")
        operation = value.get("operation")
        if operation == "delete":
            if set(value) != {"operation", "path"}:
                raise ValueError("delete changes must contain exactly operation and path")
            return cls.delete(_require_wiki_path(_require_posix_text(value["path"], "path")))
        if operation == "write":
            if set(value) != {"operation", "path", "staging_path", "sha256"}:
                raise ValueError("write changes must contain exactly operation, path, staging_path, and sha256")
            path = _require_wiki_path(_require_posix_text(value["path"], "path"))
            staging = _require_staging_path(
                _require_posix_text(value["staging_path"], "staging_path"), path
            )
            return cls.write(path, staging, _require_hex(value["sha256"], "sha256"))
        raise ValueError("manifest change operation must be write or delete")


def _validate_proof(value: LinkCandidateRunProof) -> None:
    if not isinstance(value, LinkCandidateRunProof):
        raise ValueError("link candidate proof has an invalid type")
    if type(value.run_id) is not str or not _SEARCH_RUN_ID.fullmatch(value.run_id):
        raise ValueError("link candidate proof run_id is invalid")
    _require_hex(value.corpus_revision, "link candidate proof corpus_revision")
    _require_wiki_path(value.page_path, "link candidate proof page_path")
    if not isinstance(value.terms, tuple) or not value.terms:
        raise ValueError("link candidate proof terms must be ordered-unique nonempty text")
    if any(
        type(term) is not str
        or not term.strip()
        or any(character in term for character in "\r\n\0")
        for term in value.terms
    ):
        raise ValueError("link candidate proof terms must be ordered-unique nonempty text")
    if len(value.terms) != len(set(value.terms)):
        raise ValueError("link candidate proof terms must be ordered-unique nonempty text")
    _require_hex(value.candidate_manifest_sha256, "link candidate proof manifest checksum")
    if (
        type(value.page_count) is not int
        or value.page_count < 1
        or type(value.candidate_count) is not int
        or value.candidate_count < 0
    ):
        raise ValueError("link candidate proof counts are invalid")


def _proof_dict(value: LinkCandidateRunProof) -> dict[str, object]:
    _validate_proof(value)
    return {
        "run_id": value.run_id,
        "corpus_revision": value.corpus_revision,
        "page_path": value.page_path.as_posix(),
        "terms": list(value.terms),
        "candidate_manifest_sha256": value.candidate_manifest_sha256,
        "page_count": value.page_count,
        "candidate_count": value.candidate_count,
    }


def _proof_from_dict(value: object) -> LinkCandidateRunProof:
    if not isinstance(value, dict) or set(value) != {
        "run_id",
        "corpus_revision",
        "page_path",
        "terms",
        "candidate_manifest_sha256",
        "page_count",
        "candidate_count",
    }:
        raise ValueError("link candidate proof must contain exactly the documented fields")
    terms = value["terms"]
    if not isinstance(terms, list):
        raise ValueError("link candidate proof terms must be an array")
    proof = LinkCandidateRunProof(
        value["run_id"],
        value["corpus_revision"],
        _require_wiki_path(_require_posix_text(value["page_path"], "link candidate proof page_path"), "link candidate proof page_path"),
        tuple(terms),
        value["candidate_manifest_sha256"],
        value["page_count"],
        value["candidate_count"],
    )
    _validate_proof(proof)
    return proof


@dataclass(frozen=True)
class WikiManifest:
    schema_version: Literal[1]
    expected_corpus_revision: str
    change_intent: ChangeIntent
    approval_event_id: str | None
    citation_rewrites: tuple[CitationRewrite, ...]
    link_candidate_runs: tuple[LinkCandidateRunProof, ...]
    changes: tuple[WikiChange, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("wiki manifest schema_version must be 1")
        _require_hex(self.expected_corpus_revision, "expected_corpus_revision")
        if type(self.change_intent) is not str or self.change_intent not in _INTENTS:
            raise ValueError("wiki manifest change_intent is invalid")
        if self.approval_event_id is not None and (
            type(self.approval_event_id) is not str
            or not self.approval_event_id.strip()
            or any(character in self.approval_event_id for character in "\r\n\0")
        ):
            raise ValueError("approval_event_id must be null or nonempty single-line text")
        if not isinstance(self.citation_rewrites, tuple) or any(
            not isinstance(item, CitationRewrite) for item in self.citation_rewrites
        ):
            raise ValueError("citation_rewrites must be a tuple of CitationRewrite values")
        rewrite_keys = tuple(
            (item.source_id, item.content_sha256, item.raw_path.as_posix())
            for item in self.citation_rewrites
        )
        if rewrite_keys != tuple(sorted(rewrite_keys)):
            raise ValueError("citation_rewrites must be strictly sorted")
        source_version_keys = tuple((item.source_id, item.content_sha256) for item in self.citation_rewrites)
        if len(set(source_version_keys)) != len(source_version_keys):
            raise ValueError("citation_rewrites cannot contain duplicate source/version keys")
        if not isinstance(self.link_candidate_runs, tuple):
            raise ValueError("link_candidate_runs must be a tuple")
        for proof in self.link_candidate_runs:
            _validate_proof(proof)
        proof_paths = tuple(proof.page_path.as_posix() for proof in self.link_candidate_runs)
        if proof_paths != tuple(sorted(proof_paths)) or len(set(proof_paths)) != len(proof_paths):
            raise ValueError("link_candidate_runs must be strictly sorted by unique page_path")
        if not isinstance(self.changes, tuple) or any(
            not isinstance(change, WikiChange) for change in self.changes
        ):
            raise ValueError("changes must be a tuple of WikiChange values")
        change_paths = tuple(change.path.as_posix() for change in self.changes)
        if len(set(change_paths)) != len(change_paths):
            raise ValueError("changes cannot contain duplicate target paths")
        runs = {
            _staging_run(change.staging_path)
            for change in self.changes
            if change.staging_path is not None
        }
        if len(runs) > 1:
            raise ValueError("all write staging paths must use the same staging run")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "expected_corpus_revision": self.expected_corpus_revision,
            "change_intent": self.change_intent,
            "approval_event_id": self.approval_event_id,
            "citation_rewrites": [item.to_dict() for item in self.citation_rewrites],
            "link_candidate_runs": [
                _proof_dict(item) for item in self.link_candidate_runs
            ],
            "changes": [item.to_dict() for item in self.changes],
        }

    def to_json(self) -> str:
        _require_wire_approval_pair(self)
        _require_complete_proof_coverage(self)
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> "WikiManifest":
        if type(text) is not str:
            raise ValueError("wiki manifest JSON must be text")
        try:
            document = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"invalid wiki manifest JSON: {error}") from error
        if not isinstance(document, dict):
            raise ValueError("wiki manifest JSON root must be an object")
        expected = {
            "schema_version",
            "expected_corpus_revision",
            "change_intent",
            "approval_event_id",
            "citation_rewrites",
            "link_candidate_runs",
            "changes",
        }
        if set(document) != expected:
            raise ValueError("wiki manifest contains unknown or missing fields")
        rewrites = document["citation_rewrites"]
        proofs = document["link_candidate_runs"]
        changes = document["changes"]
        if not isinstance(rewrites, list) or not isinstance(proofs, list) or not isinstance(changes, list):
            raise ValueError("wiki manifest collection fields must be arrays")
        try:
            parsed_rewrites = tuple(
                CitationRewrite(
                    item["source_id"],
                    item["content_sha256"],
                    _require_posix_text(item["raw_path"], "citation rewrite raw_path"),
                )
                if isinstance(item, dict)
                and set(item) == {"source_id", "content_sha256", "raw_path"}
                else _invalid_rewrite()
                for item in rewrites
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid citation rewrite: {error}") from error
        manifest = cls(
            document["schema_version"],
            document["expected_corpus_revision"],
            document["change_intent"],
            document["approval_event_id"],
            parsed_rewrites,
            tuple(_proof_from_dict(item) for item in proofs),
            tuple(WikiChange.from_dict(item) for item in changes),
        )
        _require_wire_approval_pair(manifest)
        _require_complete_proof_coverage(manifest)
        return manifest


def _invalid_rewrite() -> CitationRewrite:
    raise ValueError("citation rewrite must contain exactly source_id, content_sha256, and raw_path")


def _require_complete_proof_coverage(manifest: WikiManifest) -> None:
    proof_paths = {proof.page_path.as_posix() for proof in manifest.link_candidate_runs}
    change_paths = {change.path.as_posix() for change in manifest.changes}
    if proof_paths != change_paths:
        raise ValueError("link candidate proofs must name exactly every explicit change path")


def _require_wire_approval_pair(manifest: WikiManifest) -> None:
    """Enforce the persisted v1 approval shape without narrowing in-memory plans.

    Agents may build an in-memory destructive plan without an approval so
    ``apply_wiki_manifest`` can stop at its prepare-and-ask gate.  Persisted
    manifests, however, must make that audit state unambiguous: destructive
    intents carry an ID and routine/rename work carries null.
    """

    needs_approval = manifest.change_intent in _APPROVAL_INTENTS
    has_approval = manifest.approval_event_id is not None
    if needs_approval and not has_approval:
        raise ValueError(
            "approval_event_id must be nonempty for destructive manifest intents"
        )
    if not needs_approval and has_approval:
        raise ValueError(
            "approval_event_id must be null for routine or rename manifest intents"
        )


class WikiTransactionError(RuntimeError):
    """A transaction could not safely reach a durable all-or-recover state."""


class ApprovalRequired(WikiTransactionError):
    """A curator must attach the recorded approval for a destructive change."""


class CorpusRevisionChanged(WikiTransactionError):
    """The source ledger changed after a manifest was prepared."""


class InvalidWikiJournal(WikiTransactionError):
    """The recovery journal is malformed or does not describe current bytes."""


class WikiPreflightError(WikiTransactionError):
    """The complete candidate wiki postimage did not validate."""


class LinkCandidateCoverageError(WikiTransactionError):
    """A changed record has no retained, complete, current candidate proof."""

    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


@dataclass(frozen=True)
class WikiApplyResult:
    corpus_revision: str
    changed_paths: tuple[PurePosixPath, ...]
    index_path: PurePosixPath
    recovered: bool


@dataclass(frozen=True)
class WikiRecoveryResult:
    recovered: bool
    restored_paths: tuple[PurePosixPath, ...]


_INDEX_PATH = PurePosixPath("wiki/index.md")
_JOURNAL_NAME = "wiki-transaction.json"
_CLAIM_DIRECTORY = "wiki-transaction-claims"
_JOURNAL_CLAIM_NAME = ".wiki-transaction-journal-claim"
_JOURNAL_RETIRED_NAME = ".wiki-transaction-journal-retired"
_JOURNAL_RETIRED_PREFIX = ".wiki-transaction-journal-retired-"
_JOURNAL_TOMBSTONE_PREFIX = ".wiki-transaction-journal-tombstone-"
_JOURNAL_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_STAGING_BYTES = 64 * 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
_MAX_JOURNAL_ENTRIES = 10_000
_MAX_JOURNAL_ORIGINAL_BYTES = 48 * 1024 * 1024
_UNSET = object()
_DARWIN_CLONE_NOFOLLOW_ANY = 0x0008
# The destination is independently constrained to one component beneath a
# descriptor-pinned parent.  `CLONE_RESOLVE_BENEATH` is therefore redundant
# here and is unavailable in older supported Darwin SDKs; NOFOLLOW_ANY is the
# portable symlink defense for fclonefileat's destination resolution.
_DARWIN_FCLONE_FLAGS = _DARWIN_CLONE_NOFOLLOW_ANY


class _NativeFcloneFileAt(Protocol):
    def __call__(
        self,
        source_fd: int,
        target_dir_fd: int,
        target: bytes,
        flags: int,
        /,
    ) -> int: ...


def _load_native_fclone_file_at() -> _NativeFcloneFileAt | None:
    """Load Darwin's descriptor-bound, no-replace clone primitive."""

    if sys.platform != "darwin":
        return None
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, "fclonefileat", None)
    if function is None:
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


_NATIVE_FCLONE_FILE_AT = _load_native_fclone_file_at()


def _require_private_claim_area(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise UnsafeFilesystemError(
            "wiki transaction claim area must be an exclusively owned mode 0700 directory"
        )


def _validate_private_claim_area(claims: _PinnedDirectory) -> None:
    _require_private_claim_area(os.fstat(claims.descriptor))
    claims.validate()


def _ensure_claim_area(paths: RepoPaths) -> Path:
    """Create the transaction-owned, non-logical claim directory if needed."""

    brain_path = paths.root / ".brain"
    with _PinnedDirectory.open(brain_path) as brain:
        brain_descriptor = brain.descriptor
        try:
            os.mkdir(_CLAIM_DIRECTORY, 0o700, dir_fd=brain_descriptor)
        except FileExistsError:
            pass
        else:
            _fsync_directory(brain_descriptor)
        claims_descriptor = brain.open_child(brain_descriptor, _CLAIM_DIRECTORY)
        _require_private_claim_area(os.fstat(claims_descriptor))
        brain.validate()
    return brain_path / _CLAIM_DIRECTORY


def _require_publish_path(value: object, name: str = "path") -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise ValueError(f"{name} must be a repository-relative POSIX path")
    path = _require_posix_text(value.as_posix(), name)
    if path == _INDEX_PATH:
        return path
    return _require_wiki_path(path, name)


def _target_parent(paths: RepoPaths, logical_path: PurePosixPath) -> tuple[Path, str]:
    if (
        not paths.root.is_absolute()
        or any(part in {".", ".."} for part in paths.root.parts[1:])
        or paths.wiki_pages != paths.root / "wiki/pages"
        or paths.wiki_questions != paths.root / "wiki/questions"
    ):
        raise ValueError("wiki roots must be canonical descendants of the repository root")
    path = _require_publish_path(logical_path, "target path")
    if path == _INDEX_PATH:
        return paths.root / "wiki", "index.md"
    if path.parts[1] == "pages":
        return paths.wiki_pages, path.name
    return paths.wiki_questions, path.name


def _target_absolute(paths: RepoPaths, logical_path: PurePosixPath) -> Path:
    parent, name = _target_parent(paths, logical_path)
    return parent / name


def _read_target_bytes(
    paths: RepoPaths, logical_path: PurePosixPath, *, label: str = "wiki target"
) -> bytes | None:
    parent, name = _target_parent(paths, logical_path)
    with _PinnedDirectory.open(parent) as pinned:
        try:
            payload, metadata = _read_regular_at(
                pinned.descriptor,
                name,
                label=label,
                max_bytes=_MAX_STAGING_BYTES,
            )
        except FileNotFoundError:
            payload = None
        else:
            if metadata.st_nlink != 1:
                raise ValueError("wiki target must be a single-link regular file")
        pinned.validate()
    return payload


def _decode_utf8(payload: bytes, label: str) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not strict UTF-8") from error


def _staged_write_texts(
    paths: RepoPaths,
    manifest: WikiManifest,
    *,
    required_run: str | None = None,
) -> dict[PurePosixPath, str]:
    """Re-read every declared staging file through its pinned parent directory."""

    result: dict[PurePosixPath, str] = {}
    runs: set[str] = set()
    for change in manifest.changes:
        if change.operation != "write":
            continue
        assert change.staging_path is not None and change.sha256 is not None
        run = _staging_run(change.staging_path)
        runs.add(run)
        if required_run is not None and run != required_run:
            raise ValueError("write staging path is not beneath the manifest staging run")
        kind = change.path.parts[1]
        parent = (
            paths.root
            / ".brain/wiki-staging"
            / run
            / "files/wiki"
            / kind
        )
        with _PinnedDirectory.open(parent) as pinned:
            payload, metadata = _read_regular_at(
                pinned.descriptor,
                change.path.name,
                label="wiki staging",
                max_bytes=_MAX_STAGING_BYTES,
            )
            if metadata.st_nlink != 1:
                raise ValueError("wiki staging file must be a single-link regular file")
            pinned.validate()
        if hashlib.sha256(payload).hexdigest() != change.sha256:
            raise ValueError("wiki staging checksum does not match the manifest")
        result[change.path] = _decode_utf8(payload, "wiki staging file")
    if len(runs) > 1:
        raise ValueError("all write staging paths must use the same staging run")
    return result


def _manifest_relative_path(paths: RepoPaths, manifest_path: Path) -> tuple[PurePosixPath, str]:
    if not isinstance(manifest_path, Path) or not manifest_path.is_absolute():
        raise ValueError("wiki manifest path must be an absolute repository path")
    try:
        relative = PurePosixPath(manifest_path.relative_to(paths.root).as_posix())
    except ValueError as error:
        raise ValueError("wiki manifest path must be within the repository") from error
    relative = _require_posix_text(relative.as_posix(), "wiki manifest path")
    if (
        len(relative.parts) != 4
        or relative.parts[:2] != (".brain", "wiki-staging")
        or not _RUN_ID.fullmatch(relative.parts[2])
        or relative.parts[3] != "manifest.json"
    ):
        raise ValueError(
            "wiki manifest must be exactly .brain/wiki-staging/wstg_<id>/manifest.json"
        )
    return relative, relative.parts[2]


def load_wiki_manifest(paths: RepoPaths, manifest_path: Path) -> WikiManifest:
    """Load a strictly bounded staged manifest and prove every staged write now."""

    _relative, run = _manifest_relative_path(paths, manifest_path)
    directory = paths.root / ".brain/wiki-staging" / run
    with _PinnedDirectory.open(directory) as pinned:
        payload, metadata = _read_regular_at(
            pinned.descriptor,
            "manifest.json",
            label="wiki manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        if metadata.st_nlink != 1:
            raise ValueError("wiki manifest must be a single-link regular file")
        pinned.validate()
    manifest = WikiManifest.from_json(_decode_utf8(payload, "wiki manifest"))
    _staged_write_texts(paths, manifest, required_run=run)
    return manifest


def _journal_exists(paths: RepoPaths) -> bool:
    """Observe only journal presence; this deliberately never parses or repairs it."""

    try:
        with _PinnedDirectory.open(paths.root / ".brain") as pinned:
            exists = any(
                _journal_name_exists(pinned.descriptor, name)
                for name in (
                    _JOURNAL_NAME,
                    _JOURNAL_CLAIM_NAME,
                    _JOURNAL_RETIRED_NAME,
                )
            )
            pinned.validate()
            return exists
    except FileNotFoundError:
        return False


def _journal_name_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def validate_wiki_transaction_state(
    paths: RepoPaths, *, corpus_revision: str
) -> ValidationReport:
    """Report a pending transaction without acquiring locks or changing files."""

    _require_hex(corpus_revision, "corpus_revision")
    if not _journal_exists(paths):
        return ValidationReport(("wiki-transaction",), (), corpus_revision)
    issue = ValidationIssue(
        "error",
        "wiki_transaction_pending",
        "A wiki transaction is pending; run ./brain --json wiki recover.",
        PurePosixPath(".brain/wiki-transaction.json"),
    )
    return ValidationReport(("wiki-transaction",), (issue,), corpus_revision)


@dataclass(frozen=True)
class _JournalEntry:
    path: PurePosixPath
    prior_exists: bool
    old_sha256: str | None
    new_sha256: str | None
    original_bytes: bytes | None

    def __post_init__(self) -> None:
        _require_publish_path(self.path, "journal path")
        if type(self.prior_exists) is not bool:
            raise ValueError("journal prior_exists must be boolean")
        if self.prior_exists:
            if self.original_bytes is None or self.old_sha256 is None:
                raise ValueError("journal existing target requires its old bytes and hash")
            _require_hex(self.old_sha256, "journal old_sha256")
            if hashlib.sha256(self.original_bytes).hexdigest() != self.old_sha256:
                raise ValueError("journal old_sha256 does not match original bytes")
        elif self.original_bytes is not None or self.old_sha256 is not None:
            raise ValueError("journal absent target cannot contain prior bytes or hash")
        if self.new_sha256 is not None:
            _require_hex(self.new_sha256, "journal new_sha256")
        if not self.prior_exists and self.new_sha256 is None:
            raise ValueError("journal cannot publish a no-op absent deletion")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path.as_posix(),
            "prior_exists": self.prior_exists,
            "old_sha256": self.old_sha256,
            "new_sha256": self.new_sha256,
            "original_b64": (
                None
                if self.original_bytes is None
                else base64.b64encode(self.original_bytes).decode("ascii")
            ),
        }


@dataclass(frozen=True)
class _RegularSnapshot:
    payload: bytes
    metadata: os.stat_result


def _regular_snapshot_matches(
    snapshot: _RegularSnapshot,
    payload: bytes,
    metadata: os.stat_result,
    *,
    require_single_link: bool,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and (not require_single_link or metadata.st_nlink == 1)
        and snapshot.payload == payload
        and snapshot.metadata.st_dev == metadata.st_dev
        and snapshot.metadata.st_ino == metadata.st_ino
        and snapshot.metadata.st_mode == metadata.st_mode
    )


def _snapshot_regular(
    payload: bytes, metadata: os.stat_result, *, require_single_link: bool
) -> _RegularSnapshot:
    if not stat.S_ISREG(metadata.st_mode) or (
        require_single_link and metadata.st_nlink != 1
    ):
        raise ValueError("transaction file must be a regular single-link file")
    return _RegularSnapshot(payload, metadata)


def _journal_entry_from_dict(value: object) -> _JournalEntry:
    expected = {
        "path",
        "prior_exists",
        "old_sha256",
        "new_sha256",
        "original_b64",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("journal entry must contain exactly the documented fields")
    path = _require_publish_path(
        _require_posix_text(value["path"], "journal path"), "journal path"
    )
    if type(value["prior_exists"]) is not bool:
        raise ValueError("journal prior_exists must be boolean")
    old = value["old_sha256"]
    new = value["new_sha256"]
    if old is not None:
        _require_hex(old, "journal old_sha256")
    if new is not None:
        _require_hex(new, "journal new_sha256")
    encoded = value["original_b64"]
    if encoded is None:
        original = None
    elif type(encoded) is str:
        try:
            original = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeError, ValueError) as error:
            raise ValueError("journal original_b64 is invalid") from error
        if base64.b64encode(original).decode("ascii") != encoded:
            raise ValueError("journal original_b64 is not canonical base64")
    else:
        raise ValueError("journal original_b64 must be null or canonical base64")
    return _JournalEntry(path, value["prior_exists"], old, new, original)


def _journal_payload(entries: tuple[_JournalEntry, ...]) -> bytes:
    if not entries or len(entries) > _MAX_JOURNAL_ENTRIES:
        raise ValueError("journal must contain a bounded nonempty entry list")
    paths = tuple(entry.path.as_posix() for entry in entries)
    if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
        raise ValueError("journal entries must have sorted unique target paths")
    total = sum(len(entry.original_bytes or b"") for entry in entries)
    if total > _MAX_JOURNAL_ORIGINAL_BYTES:
        raise ValueError("journal original bytes exceed the supported bound")
    payload = _canonical_json(
        {
            "schema_version": _JOURNAL_SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in entries],
        }
    ).encode("utf-8")
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise ValueError("journal exceeds the supported byte bound")
    return payload


def _decode_journal(payload: bytes) -> tuple[_JournalEntry, ...]:
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise InvalidWikiJournal("wiki transaction journal exceeds the supported bound")
    try:
        text = _decode_utf8(payload, "wiki transaction journal")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise InvalidWikiJournal(f"wiki transaction journal is invalid: {error}") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "entries"}:
        raise InvalidWikiJournal("wiki transaction journal has an invalid schema")
    if type(document["schema_version"]) is not int or document["schema_version"] != _JOURNAL_SCHEMA_VERSION:
        raise InvalidWikiJournal("wiki transaction journal schema_version is invalid")
    values = document["entries"]
    if not isinstance(values, list):
        raise InvalidWikiJournal("wiki transaction journal entries must be an array")
    try:
        entries = tuple(_journal_entry_from_dict(value) for value in values)
        expected = _journal_payload(entries)
    except ValueError as error:
        raise InvalidWikiJournal(f"wiki transaction journal is invalid: {error}") from error
    if payload != expected:
        raise InvalidWikiJournal("wiki transaction journal is not canonical")
    return entries


def _read_journal(
    paths: RepoPaths,
) -> tuple[tuple[_JournalEntry, ...], _RegularSnapshot] | None:
    try:
        with _PinnedDirectory.open(paths.root / ".brain") as pinned:
            found: list[tuple[str, bytes, _RegularSnapshot]] = []
            for name in (
                _JOURNAL_NAME,
                _JOURNAL_CLAIM_NAME,
                _JOURNAL_RETIRED_NAME,
            ):
                try:
                    payload, metadata = _read_regular_at(
                        pinned.descriptor,
                        name,
                        label="wiki transaction journal",
                        max_bytes=_MAX_JOURNAL_BYTES,
                    )
                except FileNotFoundError:
                    continue
                found.append(
                    (
                        name,
                        payload,
                        _snapshot_regular(
                            payload, metadata, require_single_link=True
                        ),
                    )
                )
            if not found:
                return None
            if len(found) != 1:
                raise InvalidWikiJournal(
                    "wiki transaction journal has conflicting claimed copies"
                )
            name, payload, snapshot = found[0]
            if name != _JOURNAL_NAME:
                try:
                    _restore_claimed_regular(pinned, name, pinned, _JOURNAL_NAME)
                    payload, metadata = _read_regular_at(
                        pinned.descriptor,
                        _JOURNAL_NAME,
                        label="wiki transaction journal",
                        max_bytes=_MAX_JOURNAL_BYTES,
                    )
                    snapshot = _snapshot_regular(
                        payload, metadata, require_single_link=True
                    )
                except (OSError, ValueError) as error:
                    raise InvalidWikiJournal(
                        "claimed wiki transaction journal cannot be restored safely"
                    ) from error
            pinned.validate()
    except InvalidWikiJournal:
        raise
    except (OSError, ValueError) as error:
        raise InvalidWikiJournal(f"wiki transaction journal cannot be read safely: {error}") from error
    return _decode_journal(payload), snapshot


def _write_journal(
    paths: RepoPaths, entries: tuple[_JournalEntry, ...]
) -> _RegularSnapshot:
    payload = _journal_payload(entries)
    temporary = f".brain-tmp-wiki-journal-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: int | None = None
    with _PinnedDirectory.open(paths.root / ".brain") as pinned:
        try:
            if any(
                _journal_name_exists(pinned.descriptor, name)
                for name in (
                    _JOURNAL_NAME,
                    _JOURNAL_CLAIM_NAME,
                    _JOURNAL_RETIRED_NAME,
                )
            ):
                raise InvalidWikiJournal(
                    "a wiki transaction journal already exists before publication"
                )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=pinned.descriptor,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            pinned.validate()
            try:
                _rename_no_replace_at(pinned.descriptor, temporary, _JOURNAL_NAME)
            except FileExistsError as error:
                raise InvalidWikiJournal(
                    "a wiki transaction journal appeared during publication"
                ) from error
            observed, metadata = _read_regular_at(
                pinned.descriptor,
                _JOURNAL_NAME,
                label="wiki transaction journal",
                max_bytes=_MAX_JOURNAL_BYTES,
            )
            snapshot = _snapshot_regular(
                observed, metadata, require_single_link=True
            )
            if observed != payload:
                raise InvalidWikiJournal(
                    "wiki transaction journal changed during publication"
                )
            pinned.validate()
            _fsync_directory(pinned.descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return snapshot


def _verify_journal_snapshot(paths: RepoPaths, snapshot: _RegularSnapshot) -> None:
    with _PinnedDirectory.open(paths.root / ".brain") as pinned:
        try:
            payload, metadata = _read_regular_at(
                pinned.descriptor,
                _JOURNAL_NAME,
                label="wiki transaction journal",
                max_bytes=_MAX_JOURNAL_BYTES,
            )
        except FileNotFoundError:
            raise InvalidWikiJournal(
                "wiki transaction journal disappeared during publication"
            ) from None
        if not _regular_snapshot_matches(
            snapshot, payload, metadata, require_single_link=True
        ):
            raise InvalidWikiJournal(
                "wiki transaction journal changed during publication"
            )
        pinned.validate()


def _require_fd_bound_clone() -> _NativeFcloneFileAt:
    native = _NATIVE_FCLONE_FILE_AT
    if native is None:
        raise UnsafeFilesystemError(
            "descriptor-bound live wiki publication is unavailable on this filesystem"
        )
    return native


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write complete transaction payload")
        remaining = remaining[written:]


def _read_open_regular_descriptor(
    descriptor: int, *, label: str, max_bytes: int
) -> tuple[bytes, os.stat_result]:
    """Read one already-open payload authority without reopening its pathname."""

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise UnsafeFilesystemError(f"{label} must be a regular file")
    chunks: list[bytes] = []
    offset = 0
    while True:
        remaining = min(1_048_576, max_bytes + 1 - offset)
        if remaining <= 0:
            raise ValueError(f"{label} exceeds the byte limit")
        chunk = os.pread(descriptor, remaining, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        if offset > max_bytes:
            raise ValueError(f"{label} exceeds the byte limit")
    after = os.fstat(descriptor)
    if not (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_mode == after.st_mode
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    ):
        raise UnsafeFilesystemError(f"{label} changed while it was being read")
    return b"".join(chunks), after


def _unlink_private_payload(
    claims: _PinnedDirectory, temporary: str, descriptor: int
) -> None:
    """Turn a transaction-private claims file into an anonymous FD authority.

    ``wiki-transaction-claims`` is a 0700 directory owned exclusively by this
    transaction implementation, never a user/source namespace.  Unlinking
    there is therefore safe scratch cleanup; after it succeeds, an FD with a
    nonzero link count proves an alias existed and must not publish live bytes.
    """

    os.unlink(temporary, dir_fd=claims.descriptor)
    if os.fstat(descriptor).st_nlink != 0:
        raise UnsafeFilesystemError(
            "transaction-private publication source acquired a hard-link alias"
        )
    claims.validate()


def _open_verified_payload(
    claims: _PinnedDirectory,
    payload: bytes,
    *,
    mode: int,
    label: str,
    anonymous: bool,
) -> tuple[str, int]:
    """Write and retain a private payload while its exact descriptor stays open.

    Normal live publication uses a claims-directory authority that is made
    anonymous before cloning.  Test-only custom publishers retain its path and
    are explicitly trusted to own any pathname effects.
    """

    temporary = f".brain-tmp-wiki-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=claims.descriptor,
        )
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        observed, _metadata = _read_open_regular_descriptor(
            descriptor,
            label=label,
            max_bytes=_MAX_STAGING_BYTES,
        )
        if observed != payload:
            raise UnsafeFilesystemError(f"{label} changed before publication")
        if anonymous:
            _unlink_private_payload(claims, temporary, descriptor)
        claims.validate()
        return temporary, descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _fclone_from_descriptor(
    source_descriptor: int,
    target_parent: _PinnedDirectory,
    target: str,
    *,
    expected_payload: bytes,
) -> None:
    """Create an absent live name atomically from an already-open payload FD."""

    if not target or "/" in target or "\0" in target:
        raise ValueError("transaction publication name must be a canonical component")
    source = os.fstat(source_descriptor)
    if not stat.S_ISREG(source.st_mode) or source.st_nlink != 0:
        raise UnsafeFilesystemError("transaction publication source must be regular")
    # This is deliberately the final source-integrity gate before the native
    # clone: a retained pathname may have been renamed, hard-linked, or
    # modified since its initial fsync.  The FD is the authority, and both its
    # exact bytes and single-link state must still match the intended payload.
    observed, observed_metadata = _read_open_regular_descriptor(
        source_descriptor,
        label="transaction publication source",
        max_bytes=_MAX_STAGING_BYTES,
    )
    if observed_metadata.st_nlink != 0 or observed != expected_payload:
        raise UnsafeFilesystemError(
            "transaction publication source changed before descriptor-bound clone"
        )
    native = _require_fd_bound_clone()
    target_parent.validate()
    ctypes.set_errno(0)
    result = native(
        source_descriptor,
        target_parent.descriptor,
        os.fsencode(target),
        _DARWIN_FCLONE_FLAGS,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        error = OSError(error_number, os.strerror(error_number), target)
        raise error


def _restore_live_snapshot(
    claims: _PinnedDirectory,
    target_parent: _PinnedDirectory,
    target: str,
    snapshot: _RegularSnapshot,
) -> None:
    """Restore an absent live target from captured bytes, not a claim pathname."""

    mode = stat.S_IMODE(snapshot.metadata.st_mode)
    _temporary, descriptor = _open_verified_payload(
        claims,
        snapshot.payload,
        mode=mode,
        label="claimed wiki target restoration payload",
        anonymous=True,
    )
    try:
        _fclone_from_descriptor(
            descriptor,
            target_parent,
            target,
            expected_payload=snapshot.payload,
        )
        restored, metadata = _read_regular_at(
            target_parent.descriptor,
            target,
            label="wiki target",
            max_bytes=_MAX_STAGING_BYTES,
        )
        if restored != snapshot.payload or metadata.st_nlink != 1:
            raise UnsafeFilesystemError(
                "restored wiki target changed during transaction recovery"
            )
        target_parent.validate()
        _fsync_directory(target_parent.descriptor)
    except FileExistsError as error:
        raise UnsafeFilesystemError(
            "claimed transaction file retained; canonical live target was repopulated"
        ) from error
    finally:
        os.close(descriptor)


def _restore_claim_from_snapshot(
    snapshot: _RegularSnapshot,
) -> Callable[[_PinnedDirectory, str, _PinnedDirectory, str], None]:
    """Bind failed claim recovery to the bytes captured before the claim move."""

    def restore(
        claim_parent: _PinnedDirectory,
        _quarantine: str,
        target_parent: _PinnedDirectory,
        canonical: str,
    ) -> None:
        _restore_live_snapshot(claim_parent, target_parent, canonical, snapshot)

    return restore


def _move_no_replace(
    source_parent: _PinnedDirectory,
    source: str,
    target_parent: _PinnedDirectory,
    target: str,
) -> None:
    if any(not name or "/" in name or "\0" in name for name in (source, target)):
        raise ValueError("transaction claim names must be canonical path components")
    source_parent.validate()
    target_parent.validate()
    if source_parent.descriptor == target_parent.descriptor:
        _rename_no_replace_at(source_parent.descriptor, source, target)
    else:
        native = _NATIVE_RENAME_NO_REPLACE
        if native is None:
            raise UnsafeFilesystemError(
                "atomic no-replace transaction quarantine is unavailable"
            )
        function, flag = native
        ctypes.set_errno(0)
        result = function(
            source_parent.descriptor,
            os.fsencode(source),
            target_parent.descriptor,
            os.fsencode(target),
            flag,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            error = OSError(error_number, os.strerror(error_number), source)
            error.filename2 = target
            raise error
    _fsync_directory(source_parent.descriptor)
    if source_parent.descriptor != target_parent.descriptor:
        _fsync_directory(target_parent.descriptor)
    source_parent.validate()
    target_parent.validate()


def _restore_claimed_regular(
    claim_parent: _PinnedDirectory,
    quarantine: str,
    target_parent: _PinnedDirectory,
    canonical: str,
) -> None:
    try:
        _move_no_replace(claim_parent, quarantine, target_parent, canonical)
    except FileExistsError as error:
        raise UnsafeFilesystemError(
            f"claimed transaction file retained as {quarantine!r}; canonical name was repopulated"
        ) from error
    except OSError as error:
        raise UnsafeFilesystemError(
            f"claimed transaction file retained as {quarantine!r}; safe restoration failed"
        ) from error


def _claim_regular(
    pinned: _PinnedDirectory,
    name: str,
    snapshot: _RegularSnapshot,
    *,
    label: str,
    require_single_link: bool,
    claim_parent: _PinnedDirectory | None = None,
    quarantine_name: str | None = None,
    failed_claim_restore: Callable[
        [_PinnedDirectory, str, _PinnedDirectory, str], None
    ] | None = None,
) -> str:
    """Move a verified name aside before deletion so it cannot target a successor."""

    destination = pinned if claim_parent is None else claim_parent
    quarantine = (
        f".brain-tmp-claim-{os.getpid()}-{uuid.uuid4().hex}"
        if quarantine_name is None
        else quarantine_name
    )
    try:
        payload, metadata = _read_regular_at(
            pinned.descriptor,
            name,
            label=label,
            max_bytes=_MAX_JOURNAL_BYTES,
        )
        if not _regular_snapshot_matches(
            snapshot,
            payload,
            metadata,
            require_single_link=require_single_link,
        ):
            raise UnsafeFilesystemError(f"{label} changed before it could be claimed")
        _move_no_replace(pinned, name, destination, quarantine)
    except (FileNotFoundError, FileExistsError, OSError) as error:
        raise UnsafeFilesystemError(f"{label} changed before it could be claimed") from error
    try:
        payload, metadata = _read_regular_at(
            destination.descriptor,
            quarantine,
            label=label,
            max_bytes=_MAX_JOURNAL_BYTES,
        )
        matches = _regular_snapshot_matches(
            snapshot,
            payload,
            metadata,
            require_single_link=require_single_link,
        )
    except BaseException as error:
        try:
            if failed_claim_restore is None:
                _restore_claimed_regular(destination, quarantine, pinned, name)
            else:
                failed_claim_restore(destination, quarantine, pinned, name)
        except BaseException as restore_error:
            error.add_note(
                f"claimed {label} restoration failed: {type(restore_error).__name__}: {restore_error}"
            )
        raise
    if not matches:
        try:
            if failed_claim_restore is None:
                _restore_claimed_regular(destination, quarantine, pinned, name)
            else:
                failed_claim_restore(destination, quarantine, pinned, name)
        except BaseException as error:
            raise UnsafeFilesystemError(
                f"claimed {label} differs from its verified snapshot and could not be restored"
            ) from error
        raise UnsafeFilesystemError(f"{label} changed before it could be claimed")
    pinned.validate()
    destination.validate()
    return quarantine


def _discard_claimed_regular(
    pinned: _PinnedDirectory,
    quarantine: str,
    snapshot: _RegularSnapshot,
    *,
    label: str,
    require_single_link: bool,
    retire_name: str | None = None,
) -> None:
    """Retain a claimed predecessor rather than unlinking a raced filename.

    POSIX exposes no unlink-by-inode primitive.  Even after a final descriptor
    read, an uncooperative writer can replace a pathname before ``unlink`` and
    make that syscall remove the successor.  The initial no-replace claim has
    already durably removed the predecessor from the live namespace, so retain
    that private quarantine as forensic transaction state instead of taking an
    unsafe final-name deletion step.
    """

    # The arguments document the snapshot bound to this retained file.  Do not
    # re-open, move, or unlink its name: any such final pathname operation would
    # reintroduce the successor race this function closes.
    del pinned, quarantine, snapshot, label, require_single_link, retire_name


def _remove_journal(paths: RepoPaths, snapshot: _RegularSnapshot) -> None:
    """Retire a completed journal through one final trusted-internal move.

    ``.brain``'s journal and tombstone names are transaction-owned control
    state, like ``wiki-transaction-claims``; they are not logical wiki/source
    inputs.  After the canonical journal has been proven exact, its direct
    no-replace move to a unique retained tombstone is the cleanup commit point.
    The tombstone is deliberately never read, moved, or unlinked afterward:
    doing so would create another pathname race for data that no longer needs
    recovery.  Journal discovery enumerates only canonical and fixed active
    names, so retained tombstones cannot block a later transaction.

    Directory fsync/anchor-close errors after that move are best effort.  The
    operation has already committed to a clean state, so they must not report
    a false pending cleanup failure.  Every operation capable of rejecting the
    exact canonical journal occurs before the final move.
    """

    tombstone = f"{_JOURNAL_TOMBSTONE_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    # This independent verification (and its descriptor close) is explicitly
    # pre-commit.  It also avoids carrying an unverified caller snapshot into
    # the cleanup-owned pinned descriptor.
    _verify_journal_snapshot(paths, snapshot)
    pinned = _PinnedDirectory.open(paths.root / ".brain")
    committed = False
    try:
        payload, metadata = _read_regular_at(
            pinned.descriptor,
            _JOURNAL_NAME,
            label="wiki transaction journal",
            max_bytes=_MAX_JOURNAL_BYTES,
        )
        if not _regular_snapshot_matches(
            snapshot, payload, metadata, require_single_link=True
        ):
            raise InvalidWikiJournal(
                "wiki transaction journal changed during cleanup"
            )
        pinned.validate()
        # Deliberately call the raw no-replace primitive rather than
        # `_move_no_replace`: its directory fsync/anchor validation belongs
        # *after* the rename and would turn a completed cleanup into an error.
        try:
            _rename_no_replace_at(pinned.descriptor, _JOURNAL_NAME, tombstone)
        except FileExistsError:
            # A unique tombstone collision is an ordinary pre-commit failure:
            # the canonical journal is still the recovery authority.
            raise
        except OSError as move_error:
            # `renameatx_np(RENAME_EXCL)` can report an indeterminate I/O
            # outcome after the namespace move reached disk.  Probe the
            # trusted internal names only to distinguish a proved completion
            # from a failed/unknown one.  Never return success on an
            # unreadable probe: a caller must not be told publication
            # succeeded while a canonical recovery journal may still exist.
            try:
                os.stat(
                    _JOURNAL_NAME,
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                # The canonical name is the only pending-state authority.  A
                # positive absence observation commits cleanup under the
                # transaction-owned `.brain` boundary; never reopen the
                # dynamic tombstone here, because a replacement/read fault
                # after the completed move must not turn clean work into an
                # error.  Tombstones are retained forensic scratch, not an
                # input to recovery or later publication.
                committed = True
            except OSError as probe_error:
                raise WikiTransactionError(
                    "final wiki journal tombstone outcome is uncertain"
                ) from probe_error
            else:
                raise WikiTransactionError(
                    "final wiki journal tombstone move failed before commit"
                ) from move_error
        else:
            committed = True
        try:
            _fsync_directory(pinned.descriptor)
            pinned.validate()
        except OSError:
            # The retained tombstone is the durable logical completion marker;
            # this best-effort checkpoint cannot safely reopen recovery.
            pass
    except BaseException as error:
        try:
            pinned.close()
        except OSError as close_error:
            if not committed:
                error.add_note(
                    "cleanup directory close failed before commit: "
                    f"{type(close_error).__name__}: {close_error}"
                )
        if committed:
            return
        raise
    try:
        pinned.close()
    except OSError:
        if not committed:
            raise


def _write_live_bytes(
    paths: RepoPaths,
    logical_path: PurePosixPath,
    payload: bytes,
    *,
    replace_file: Callable[[Path, Path], None],
    expected_current: bytes | None | object = _UNSET,
) -> None:
    """Publish through a claimed target name, never overwriting a successor."""

    # No path-based fallback is safe for the normal publisher: the final
    # source name can be swapped after validation but before a rename.  A
    # non-default callback is a deliberately trusted test fault-injection
    # seam; callers of that seam own its arbitrary pathname effects.
    if replace_file is os.replace:
        _require_fd_bound_clone()
    parent_path, name = _target_parent(paths, logical_path)
    claim_area = _ensure_claim_area(paths)
    descriptor: int | None = None
    claimed: tuple[str, _RegularSnapshot] | None = None
    published = False
    with _PinnedDirectory.open(parent_path) as pinned, _PinnedDirectory.open(
        claim_area
    ) as claims:
        _validate_private_claim_area(claims)
        try:
            try:
                current, metadata = _read_regular_at(
                    pinned.descriptor,
                    name,
                    label="wiki target",
                    max_bytes=_MAX_STAGING_BYTES,
                )
            except FileNotFoundError:
                current = None
                snapshot = None
            else:
                snapshot = _snapshot_regular(
                    current, metadata, require_single_link=True
                )
            if expected_current is not _UNSET and current != expected_current:
                raise WikiTransactionError(
                    "A live wiki target changed during publication."
                )
            temporary, descriptor = _open_verified_payload(
                claims,
                payload,
                mode=0o644,
                label="wiki publication payload",
                anonymous=replace_file is os.replace,
            )
            if snapshot is not None:
                try:
                    quarantine = _claim_regular(
                        pinned,
                        name,
                        snapshot,
                        label="wiki target",
                        require_single_link=True,
                        claim_parent=claims,
                        failed_claim_restore=_restore_claim_from_snapshot(snapshot),
                    )
                except OSError as error:
                    raise WikiTransactionError(
                        "A live wiki target changed during publication."
                    ) from error
                claimed = (quarantine, snapshot)
            pinned.validate()
            if replace_file is os.replace:
                try:
                    _fclone_from_descriptor(
                        descriptor,
                        pinned,
                        name,
                        expected_payload=payload,
                    )
                except FileExistsError as error:
                    raise WikiTransactionError(
                        "A live wiki target changed during publication."
                    ) from error
            else:
                replace_file(claim_area / temporary, parent_path / name)
            observed, observed_metadata = _read_regular_at(
                pinned.descriptor,
                name,
                label="wiki target",
                max_bytes=_MAX_STAGING_BYTES,
            )
            if observed != payload or observed_metadata.st_nlink != 1:
                raise OSError("wiki target changed during publication")
            published = True
            pinned.validate()
            _fsync_directory(pinned.descriptor)
            if claimed is not None:
                quarantine, snapshot = claimed
                _discard_claimed_regular(
                    claims,
                    quarantine,
                    snapshot,
                    label="wiki target",
                    require_single_link=True,
                )
                claimed = None
        except BaseException as error:
            if claimed is not None and not published:
                quarantine, snapshot = claimed
                failure_observed: bytes | None | object = _UNSET
                try:
                    observed, observed_metadata = _read_regular_at(
                        pinned.descriptor,
                        name,
                        label="wiki target",
                        max_bytes=_MAX_STAGING_BYTES,
                    )
                except FileNotFoundError:
                    failure_observed = None
                except BaseException as observation_error:
                    error.add_note(
                        "live target could not be observed after failed publication: "
                        f"{type(observation_error).__name__}: {observation_error}"
                    )
                else:
                    failure_observed = observed
                    if observed == payload and observed_metadata.st_nlink == 1:
                        try:
                            _discard_claimed_regular(
                                claims,
                                quarantine,
                                snapshot,
                                label="wiki target",
                                require_single_link=True,
                            )
                        except BaseException as cleanup_error:
                            error.add_note(
                                "prior target cleanup after failed publication failed: "
                                f"{type(cleanup_error).__name__}: {cleanup_error}"
                            )
                        else:
                            claimed = None
                if claimed is not None and failure_observed is None:
                    try:
                        _restore_live_snapshot(claims, pinned, name, snapshot)
                    except BaseException as restore_error:
                        error.add_note(
                            "claimed target restoration failed: "
                            f"{type(restore_error).__name__}: {restore_error}"
                        )
                    else:
                        claimed = None
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _delete_live_target(
    paths: RepoPaths,
    logical_path: PurePosixPath,
    *,
    expected_bytes: bytes | None,
    expected_sha256: str | None,
    allow_absent: bool,
    require_absent: bool = False,
    error_type: type[WikiTransactionError] = WikiTransactionError,
) -> None:
    """Claim the verified target before unlinking so a successor is never removed."""

    _require_fd_bound_clone()
    parent_path, name = _target_parent(paths, logical_path)
    claim_area = _ensure_claim_area(paths)
    with _PinnedDirectory.open(parent_path) as pinned, _PinnedDirectory.open(
        claim_area
    ) as claims:
        _validate_private_claim_area(claims)
        try:
            payload, metadata = _read_regular_at(
                pinned.descriptor,
                name,
                label="wiki target",
                max_bytes=_MAX_STAGING_BYTES,
            )
        except FileNotFoundError:
            pinned.validate()
            if allow_absent:
                return
            raise error_type("A live wiki target disappeared before publication.") from None
        if require_absent:
            raise error_type("A live wiki target appeared during recovery.")
        if expected_bytes is not None and payload != expected_bytes:
            raise error_type("A live wiki target changed during publication.")
        if expected_sha256 is not None and (
            hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise error_type("A live wiki target changed during publication.")
        snapshot = _snapshot_regular(
            payload, metadata, require_single_link=True
        )
        try:
            quarantine = _claim_regular(
                pinned,
                name,
                snapshot,
                label="wiki target",
                require_single_link=True,
                claim_parent=claims,
                failed_claim_restore=_restore_claim_from_snapshot(snapshot),
            )
        except OSError as error:
            raise error_type("A live wiki target changed during publication.") from error
        try:
            _discard_claimed_regular(
                claims,
                quarantine,
                snapshot,
                label="wiki target",
                require_single_link=True,
            )
        except BaseException as error:
            try:
                _restore_live_snapshot(claims, pinned, name, snapshot)
            except BaseException as restore_error:
                error.add_note(
                    f"claimed target restoration failed: {type(restore_error).__name__}: {restore_error}"
                )
            raise


def _coverage_error(change: WikiChange, error: BaseException) -> LinkCandidateCoverageError:
    return LinkCandidateCoverageError(
        Diagnostic(
            "link_candidate_search_incomplete",
            "The changed wiki record lacks a complete, current retained link-candidate search.",
            change.path,
            {"reason": str(error)},
        )
    )


def _verify_candidate_proofs_locked(
    paths: RepoPaths,
    ledger: LedgerStore,
    manifest: WikiManifest,
    corpus_revision: str,
) -> None:
    proofs = {proof.page_path: proof for proof in manifest.link_candidate_runs}
    for change in manifest.changes:
        proof = proofs.get(change.path)
        if proof is None:
            raise LinkCandidateCoverageError(
                Diagnostic(
                    "link_candidate_search_incomplete",
                    "Every explicit wiki record change requires a retained link-candidate proof.",
                    change.path,
                )
            )
        if proof.corpus_revision != corpus_revision:
            raise _coverage_error(change, ValueError("candidate proof revision is stale"))
        try:
            _verify_link_candidate_run_locked(
                paths,
                ledger,
                run_id=proof.run_id,
                corpus_revision=proof.corpus_revision,
                page_path=proof.page_path,
                terms=proof.terms,
                candidate_manifest_sha256=proof.candidate_manifest_sha256,
                page_count=proof.page_count,
                candidate_count=proof.candidate_count,
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise _coverage_error(change, error) from error


def _verify_citation_rewrites(
    records: Mapping[str, object], rewrites: tuple[CitationRewrite, ...]
) -> None:
    for rewrite in rewrites:
        record = records.get(rewrite.source_id)
        version = getattr(record, "versions", {}).get(rewrite.content_sha256)
        if version is None or getattr(version, "raw_path", None) != rewrite.raw_path:
            raise WikiPreflightError(
                "Citation rewrite does not match the retained source version path."
            )


def _rewrite_and_canonicalize_documents(
    paths: RepoPaths,
    ledger: LedgerStore,
    documents: Mapping[Path, str],
    rewrites: tuple[CitationRewrite, ...],
) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for path, text in sorted(documents.items(), key=lambda item: item[0].as_posix()):
        try:
            rewritten = rewrite_historical_original_links(
                text,
                rewrites,
                paths=paths,
                document_path=path,
            )
            result[path] = canonicalize_citation_destinations(
                rewritten,
                ledger,
                paths=paths,
                document_path=path,
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise WikiPreflightError(
                f"Citation postimage canonicalization failed for {path}: {error}"
            ) from error
    return result


@dataclass(frozen=True)
class _DocumentStructure:
    markers: Counter[str]
    definitions: Counter[str]
    relationships: frozenset[tuple[str, str]]


def _document_structure(path: Path, text: str) -> _DocumentStructure:
    scan = scan_markdown(text)
    if scan.diagnostics:
        raise WikiPreflightError(f"Wiki record has ambiguous Markdown: {path}")
    model = (
        parse_page(path, text=text)
        if path.parent.name == "pages"
        else parse_question(path, text=text)
    )
    relationships: set[tuple[str, str]] = {
        ("page", relation.destination) for relation in model.related_pages
    }
    if hasattr(model, "related_questions"):
        relationships.update(
            ("question", relation.destination) for relation in model.related_questions
        )
    return _DocumentStructure(
        Counter(marker.citation_id for marker in scan.citation_markers),
        Counter(marker.citation_id for marker in scan.citation_definitions),
        frozenset(relationships),
    )


def _reject_unapproved_structural_removals(
    manifest: WikiManifest,
    before: Mapping[Path, str],
    after: Mapping[Path, str],
) -> None:
    if manifest.change_intent not in {"routine", "rename"}:
        return
    removed_records = set(before) - set(after)
    if removed_records:
        raise ApprovalRequired(
            "Routine and rename manifests cannot remove a wiki record; use an approved destructive intent."
        )
    for path in sorted(set(before) & set(after), key=lambda item: item.as_posix()):
        prior = _document_structure(path, before[path])
        candidate = _document_structure(path, after[path])
        if any(candidate.markers[key] < value for key, value in prior.markers.items()):
            raise ApprovalRequired(
                "Routine and rename manifests cannot remove a citation marker."
            )
        if any(
            candidate.definitions[key] < value
            for key, value in prior.definitions.items()
        ):
            raise ApprovalRequired(
                "Routine and rename manifests cannot remove a citation definition."
            )
        if not prior.relationships.issubset(candidate.relationships):
            raise ApprovalRequired(
                "Routine and rename manifests cannot remove a declared relationship."
            )


def _preflight_postimage(
    paths: RepoPaths,
    ledger: LedgerStore,
    manifest: WikiManifest,
    staged_writes: Mapping[PurePosixPath, str],
    corpus_revision: str,
    records: Mapping[str, object],
) -> tuple[dict[Path, str], dict[Path, str], str, dict[PurePosixPath, bytes | None]]:
    """Build and validate one complete authoritative postimage before publication."""

    try:
        loaded = _load_live_documents(paths)
    except (OSError, UnicodeError, ValueError) as error:
        raise WikiPreflightError(f"Live wiki cannot be loaded safely: {error}") from error
    before = {path: text for path, text in loaded.values()}
    candidate = dict(before)
    for change in manifest.changes:
        target = _target_absolute(paths, change.path)
        if change.operation == "write":
            candidate[target] = staged_writes[change.path]
        else:
            if target not in candidate:
                raise WikiPreflightError("Manifest deletes a wiki record that does not exist.")
            del candidate[target]
    _verify_citation_rewrites(records, manifest.citation_rewrites)
    candidate = _rewrite_and_canonicalize_documents(
        paths, ledger, candidate, manifest.citation_rewrites
    )
    citations = validate_citations(paths, ledger, candidate, full=False)
    if not citations.ok:
        raise WikiPreflightError(
            "Candidate wiki postimage has citation validation errors: "
            + ", ".join(sorted({issue.code for issue in citations.issues}))
        )
    try:
        index_text = render_wiki_index(paths, candidate)
    except (OSError, UnicodeError, ValueError) as error:
        raise WikiPreflightError(f"Candidate wiki index cannot be rendered: {error}") from error
    graph = validate_graph(
        paths,
        documents=candidate,
        index_text=index_text,
        corpus_revision=corpus_revision,
    )
    if not graph.ok:
        raise WikiPreflightError(
            "Candidate wiki postimage has graph validation errors: "
            + ", ".join(sorted({issue.code for issue in graph.issues}))
        )
    _reject_unapproved_structural_removals(manifest, before, candidate)
    preimages = {
        _require_publish_path(
            PurePosixPath(path.relative_to(paths.root).as_posix()), "preflight path"
        ): text.encode("utf-8")
        for path, text in before.items()
    }
    preimages[_INDEX_PATH] = _read_target_bytes(paths, _INDEX_PATH, label="wiki index")
    return before, candidate, index_text, preimages


def _planned_publication(
    paths: RepoPaths,
    before: Mapping[Path, str],
    candidate: Mapping[Path, str],
    index_text: str,
    observed_index: bytes | None,
) -> tuple[tuple[PurePosixPath, bytes | None], ...]:
    planned: dict[PurePosixPath, bytes | None] = {}
    for path in sorted(set(before) | set(candidate), key=lambda item: item.as_posix()):
        logical = _require_publish_path(
            PurePosixPath(path.relative_to(paths.root).as_posix()), "postimage path"
        )
        previous = before.get(path)
        current = candidate.get(path)
        if previous == current:
            continue
        planned[logical] = None if current is None else current.encode("utf-8")
    encoded_index = index_text.encode("utf-8")
    if observed_index != encoded_index:
        planned[_INDEX_PATH] = encoded_index
    return tuple(sorted(planned.items(), key=lambda item: item[0].as_posix()))


def _snapshot_journal_entries(
    paths: RepoPaths,
    planned: tuple[tuple[PurePosixPath, bytes | None], ...],
    preimages: Mapping[PurePosixPath, bytes | None],
) -> tuple[_JournalEntry, ...]:
    expected = dict(preimages)
    for logical_path, _new_bytes in planned:
        expected.setdefault(logical_path, None)
    _verify_live_document_mapping(
        paths,
        expected,
        error_type=WikiTransactionError,
        phase="preflight",
    )
    _verify_live_preimages(paths, expected)
    entries: list[_JournalEntry] = []
    for logical_path, new_bytes in planned:
        old_bytes = _read_target_bytes(paths, logical_path)
        if old_bytes != expected[logical_path]:
            raise WikiTransactionError(
                "A live wiki target changed during preflight before journaling."
            )
        entries.append(
            _JournalEntry(
                logical_path,
                old_bytes is not None,
                None
                if old_bytes is None
                else hashlib.sha256(old_bytes).hexdigest(),
                None if new_bytes is None else hashlib.sha256(new_bytes).hexdigest(),
                old_bytes,
            )
        )
    return tuple(entries)


def _verify_live_preimages(
    paths: RepoPaths, preimages: Mapping[PurePosixPath, bytes | None]
) -> None:
    for logical_path in sorted(preimages, key=lambda path: path.as_posix()):
        if _read_target_bytes(paths, logical_path) != preimages[logical_path]:
            raise WikiTransactionError(
                "A live wiki target changed during preflight before publication."
            )


def _verify_live_document_mapping(
    paths: RepoPaths,
    expected: Mapping[PurePosixPath, bytes | None],
    *,
    error_type: type[WikiTransactionError],
    phase: str,
) -> None:
    """Prove the complete logical record namespace still matches one postimage."""

    expected_documents = {
        logical_path: payload
        for logical_path, payload in expected.items()
        if logical_path != _INDEX_PATH and payload is not None
    }
    try:
        loaded = _load_live_documents(paths)
    except (OSError, UnicodeError, ValueError) as error:
        raise error_type(
            f"The live wiki record mapping could not be verified during {phase}."
        ) from error
    observed_documents = {
        logical_path: text.encode("utf-8")
        for logical_path, (_path, text) in loaded.items()
    }
    if observed_documents != expected_documents:
        raise error_type(f"The live wiki record mapping changed during {phase}.")


def _verify_live_postimages(
    paths: RepoPaths,
    expected: Mapping[PurePosixPath, bytes | None],
    *,
    error_type: type[WikiTransactionError],
) -> None:
    """Prove the entire transaction postimage immediately before cleanup."""

    phase = "recovery" if error_type is InvalidWikiJournal else "publication"
    for logical_path in sorted(expected, key=lambda path: path.as_posix()):
        try:
            observed = _read_target_bytes(paths, logical_path)
        except (OSError, ValueError) as error:
            raise error_type(
                f"A live wiki target could not be verified during {phase}."
            ) from error
        if observed != expected[logical_path]:
            raise error_type(f"A live wiki target changed during {phase}.")


def _validate_journal_current_states(
    paths: RepoPaths, entries: tuple[_JournalEntry, ...]
) -> None:
    """Prove every entry is recoverable before mutating even the first target."""

    for entry in entries:
        _validate_journal_current_entry(paths, entry)


def _validate_journal_current_entry(
    paths: RepoPaths, entry: _JournalEntry
) -> bytes | None:
    current = _read_target_bytes(paths, entry.path)
    if current is None:
        # A claimed old target may be durably moved aside immediately before a
        # replacement or deletion. The journal owns its exact preimage, so an
        # absent canonical name is a recoverable transition for every entry.
        return None
    observed = hashlib.sha256(current).hexdigest()
    allowed = {entry.old_sha256}
    if entry.new_sha256 is not None:
        allowed.add(entry.new_sha256)
    if observed not in allowed:
        raise InvalidWikiJournal(
            f"journal target {entry.path.as_posix()} has unexpected current bytes"
        )
    return current


def _recover_locked(paths: RepoPaths) -> WikiRecoveryResult:
    journal = _read_journal(paths)
    if journal is None:
        return WikiRecoveryResult(False, ())
    entries, journal_snapshot = journal
    _validate_journal_current_states(paths, entries)
    restored: list[PurePosixPath] = []
    restored_postimage = {
        entry.path: entry.original_bytes if entry.prior_exists else None
        for entry in entries
    }
    for entry in entries:
        _verify_journal_snapshot(paths, journal_snapshot)
        current = _validate_journal_current_entry(paths, entry)
        if entry.prior_exists:
            assert entry.original_bytes is not None
            _write_live_bytes(
                paths,
                entry.path,
                entry.original_bytes,
                replace_file=os.replace,
                expected_current=current,
            )
        else:
            _delete_live_target(
                paths,
                entry.path,
                expected_bytes=current,
                expected_sha256=None,
                allow_absent=current is None,
                require_absent=current is None,
                error_type=InvalidWikiJournal,
            )
        restored.append(entry.path)
    _verify_journal_snapshot(paths, journal_snapshot)
    _verify_live_postimages(
        paths, restored_postimage, error_type=InvalidWikiJournal
    )
    _verify_journal_snapshot(paths, journal_snapshot)
    _remove_journal(paths, journal_snapshot)
    return WikiRecoveryResult(True, tuple(restored))


def recover_wiki_transaction(paths: RepoPaths) -> WikiRecoveryResult:
    """Restore a journaled preimage under the same source-then-wiki lock order."""

    with SourceWriteLock.acquire(paths.lock):
        with SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock"):
            return _recover_locked(paths)


def _checked_in_memory_manifest(manifest: WikiManifest) -> WikiManifest:
    if not isinstance(manifest, WikiManifest):
        raise ValueError("apply requires a WikiManifest")
    try:
        # In-memory agents may intentionally hand apply an incomplete proof set.
        # That is an execution-time coverage failure, not a codec bypass: all
        # ordinary manifest invariants still run here and loaded JSON remains
        # stricter through ``from_json`` above.
        manifest.__post_init__()
        return manifest
    except (TypeError, ValueError) as error:
        raise ValueError(f"wiki manifest is invalid: {error}") from error


def _apply_locked(
    paths: RepoPaths,
    ledger: LedgerStore,
    manifest: WikiManifest,
    *,
    replace_file: Callable[[Path, Path], None],
    recovered: bool,
) -> WikiApplyResult:
    checked = _checked_in_memory_manifest(manifest)
    staged_writes = _staged_write_texts(paths, checked)
    try:
        records = ledger.load_all()
        corpus_revision = compute_corpus_revision(records.values())
    except (OSError, ValueError) as error:
        raise WikiTransactionError(f"source ledger cannot be read safely: {error}") from error
    if corpus_revision != checked.expected_corpus_revision:
        raise CorpusRevisionChanged(
            "The source corpus revision changed after this wiki manifest was prepared."
        )
    _verify_candidate_proofs_locked(paths, ledger, checked, corpus_revision)
    before, candidate, index_text, preimages = _preflight_postimage(
        paths,
        ledger,
        checked,
        staged_writes,
        corpus_revision,
        records,
    )
    # The retained run is bound to the complete live wiki operand mapping.
    # Recheck after capture so the proof covers this exact preflight snapshot;
    # `_snapshot_journal_entries` then rejects any later mapping drift.
    _verify_candidate_proofs_locked(paths, ledger, checked, corpus_revision)
    if checked.change_intent in _APPROVAL_INTENTS and not checked.approval_event_id:
        raise ApprovalRequired(
            "This destructive wiki change requires a nonempty approval_event_id."
        )
    planned = _planned_publication(
        paths,
        before,
        candidate,
        index_text,
        preimages[_INDEX_PATH],
    )
    if not planned:
        return WikiApplyResult(corpus_revision, (), _INDEX_PATH, recovered)
    entries = _snapshot_journal_entries(paths, planned, preimages)
    # The postimage was built from the earlier staging read. Recheck every
    # staged file immediately before durable publication preparation so a
    # concurrent edit cannot publish stale in-memory bytes.
    _staged_write_texts(paths, checked)
    journal_snapshot = _write_journal(paths, entries)
    remaining = dict(preimages)
    published_postimage = dict(preimages)
    for logical_path, payload in planned:
        published_postimage[logical_path] = payload
    for entry, (logical_path, payload) in zip(entries, planned, strict=True):
        _verify_journal_snapshot(paths, journal_snapshot)
        _verify_live_preimages(paths, remaining)
        if payload is None:
            _delete_live_target(
                paths,
                logical_path,
                expected_bytes=entry.original_bytes,
                expected_sha256=None,
                allow_absent=False,
            )
        else:
            _write_live_bytes(
                paths,
                logical_path,
                payload,
                replace_file=replace_file,
                expected_current=entry.original_bytes,
            )
        remaining.pop(logical_path, None)
    _verify_journal_snapshot(paths, journal_snapshot)
    _verify_live_document_mapping(
        paths,
        published_postimage,
        error_type=WikiTransactionError,
        phase="publication",
    )
    _verify_live_postimages(
        paths, published_postimage, error_type=WikiTransactionError
    )
    _verify_live_document_mapping(
        paths,
        published_postimage,
        error_type=WikiTransactionError,
        phase="publication",
    )
    _verify_journal_snapshot(paths, journal_snapshot)
    _remove_journal(paths, journal_snapshot)
    return WikiApplyResult(
        corpus_revision,
        tuple(path for path, _payload in planned),
        _INDEX_PATH,
        recovered,
    )


def apply_wiki_manifest(
    paths: RepoPaths,
    ledger: LedgerStore,
    manifest: WikiManifest,
    *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> WikiApplyResult:
    """Preflight one complete postimage, then atomically publish or leave recovery data."""

    if not callable(replace_file):
        raise ValueError("replace_file must be callable")
    with SourceWriteLock.acquire(paths.lock):
        with SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock"):
            recovery = _recover_locked(paths)
            return _apply_locked(
                paths,
                ledger,
                manifest,
                replace_file=replace_file,
                recovered=recovery.recovered,
            )
