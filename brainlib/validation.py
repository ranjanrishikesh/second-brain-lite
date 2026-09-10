from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .contracts import (
    FileFingerprint,
    SourceRecord,
    SourceState,
    compute_corpus_revision,
    compute_sha256,
    derivation_id,
    source_id_for_first_seen,
)
from .diagnostics import ValidationIssue, ValidationReport
from .inventory import (
    InventoryAccessError,
    InventoryItem,
    InventoryReport,
    MediaDetector,
    SnapshotNamespace,
    StableFileSnapshot,
    inventory_raw_sources,
    is_url_descriptor_path,
    stable_file_snapshot,
    validate_inventory_path,
)
from .layout import RepoPaths
from .ledger import LedgerStore, _PinnedDirectory
from .locking import SourceWriteLock
from .registry import (
    ExtractorRegistry,
    is_committed_builtin_converter_id,
    is_normalized_identifier,
)


_REQUIRED_DIRECTORIES = (
    PurePosixPath(".agents/skills"),
    PurePosixPath(".claude/skills"),
    PurePosixPath(".codex/agents"),
    PurePosixPath(".claude/agents"),
    PurePosixPath("sources/raw/_versions"),
    PurePosixPath("sources/raw/_web"),
    PurePosixPath("sources/extracted"),
    PurePosixPath("sources/ledger"),
    PurePosixPath("wiki/pages"),
    PurePosixPath("wiki/questions"),
)

_REQUIRED_SCHEMAS = (
    PurePosixPath("docs/brain/schemas/source-record.v1.schema.json"),
    PurePosixPath("docs/brain/schemas/page-frontmatter.v1.schema.json"),
    PurePosixPath("docs/brain/schemas/question-frontmatter.v1.schema.json"),
    PurePosixPath("docs/brain/schemas/question-frontmatter.v2.schema.json"),
)

_PRISTINE_LEDGER = b"# Source Ledger\n\nNot initialized. Run `./brain init`.\n"
_SUMMARY_TIMESTAMP_RE = re.compile(
    rb"^Last synchronized: "
    rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_OPERATIONAL_WARNING_CODES = frozenset({"ambiguous_rename", "raw_missing"})


class ChecksumCache:
    """A checksum cache that is reset for, and poisoned within, one transaction."""

    def __init__(self, *, hash_file: Callable[[Path], str] = compute_sha256) -> None:
        self._hash_file = hash_file
        self._identities: dict[tuple[SnapshotNamespace, PurePosixPath], object] = {}
        self._checksums: dict[tuple[SnapshotNamespace, PurePosixPath, object], str] = {}
        self._poisoned: set[tuple[SnapshotNamespace, PurePosixPath]] = set()

    def begin_transaction(self) -> None:
        self._identities.clear()
        self._checksums.clear()
        self._poisoned.clear()

    def poison(
        self, namespace: SnapshotNamespace, logical_path: PurePosixPath
    ) -> None:
        """Retain a caller-detected authority failure until the next transaction."""
        if not isinstance(namespace, SnapshotNamespace) or not isinstance(
            logical_path, PurePosixPath
        ):
            raise ValueError(
                "cache poisoning requires a namespace and logical POSIX path"
            )
        self._poisoned.add((namespace, logical_path))

    def observe(
        self,
        paths: RepoPaths,
        namespace: SnapshotNamespace,
        logical_path: PurePosixPath,
        *,
        full: bool,
        expected_fingerprint: FileFingerprint | None = None,
    ) -> StableFileSnapshot:
        logical_key = (namespace, logical_path)
        if logical_key in self._poisoned:
            raise InventoryAccessError("file identity changed during validation")
        prior_identity = self._identities.get(logical_key)
        include_sha256 = full and prior_identity is None
        try:
            snapshot = stable_file_snapshot(
                paths,
                namespace,
                logical_path,
                include_sha256=include_sha256,
                hash_file=self._hash_file,
                expected_fingerprint=expected_fingerprint,
            )
        except InventoryAccessError:
            self._poisoned.add(logical_key)
            raise
        if prior_identity is not None and snapshot.identity != prior_identity:
            self._poisoned.add(logical_key)
            raise InventoryAccessError("file identity changed during validation")
        if prior_identity is None:
            self._identities[logical_key] = snapshot.identity
        if not full:
            return snapshot
        cache_key = (namespace, logical_path, snapshot.identity)
        if snapshot.sha256 is not None:
            self._checksums[cache_key] = snapshot.sha256
            return snapshot
        checksum = self._checksums.get(cache_key)
        if checksum is None:
            self._poisoned.add(logical_key)
            raise InventoryAccessError("checksum cache lost its stable observation")
        return replace(snapshot, sha256=checksum)

    def sha256(
        self,
        paths: RepoPaths,
        namespace: SnapshotNamespace,
        logical_path: PurePosixPath,
    ) -> str:
        snapshot = self.observe(paths, namespace, logical_path, full=True)
        assert snapshot.sha256 is not None
        return snapshot.sha256


def validate_source_ledger(
    paths: RepoPaths,
    records: Mapping[str, SourceRecord],
    *,
    full: bool = False,
    checksum_cache: ChecksumCache | None = None,
    inventory: InventoryReport | None = None,
    registry: ExtractorRegistry | None = None,
    summary_bytes: bytes | None = None,
) -> ValidationReport:
    """Validate all retained source evidence from one caller-owned snapshot."""

    issues: list[ValidationIssue] = []
    if checksum_cache is None:
        cache = ChecksumCache()
        cache.begin_transaction()
    else:
        # A supplied cache belongs to the caller's wider validation transaction.
        # Resetting it here would discard stable observations already shared with
        # citation and output validation in that same command snapshot.
        cache = checksum_cache
    report = (
        inventory_raw_sources(paths, MediaDetector())
        if inventory is None
        else inventory
    )
    # The registry governs currentness during synchronization, not retained proof.
    del registry

    valid_records: dict[str, SourceRecord] = {}
    for source_id, record in sorted(records.items()):
        if source_id != getattr(record, "source_id", None):
            issues.append(
                _ledger_error(
                    "ledger_record_key_mismatch",
                    "Ledger mapping key does not match its source_id.",
                )
            )
            continue
        try:
            record.to_dict()
        except (TypeError, ValueError) as error:
            issues.append(
                _ledger_error(
                    "ledger_record_invalid",
                    f"Ledger record is invalid: {error}",
                    PurePosixPath("sources/ledger", f"{source_id}.json"),
                )
            )
            continue
        valid_records[source_id] = record

    path_index: dict[PurePosixPath, list[str]] = {}
    for record in valid_records.values():
        path_index.setdefault(record.current_raw_path, []).append(record.source_id)
    for path, source_ids in sorted(
        path_index.items(), key=lambda item: item[0].as_posix()
    ):
        if len(source_ids) > 1:
            issues.append(
                _ledger_error(
                    "duplicate_current_raw_path",
                    "More than one ledger record names the same current raw path.",
                    path,
                    {"source_ids": sorted(source_ids)},
                )
            )

    inventory_paths: dict[PurePosixPath, int] = {}
    inventory_items: dict[PurePosixPath, InventoryItem] = {}
    for item in report.items:
        path = item.fingerprint.path
        inventory_paths[path] = inventory_paths.get(path, 0) + 1
        inventory_items.setdefault(path, item)
    for path, count in sorted(
        inventory_paths.items(), key=lambda item: item[0].as_posix()
    ):
        if count != 1:
            issues.append(
                _ledger_error(
                    "duplicate_inventory_path",
                    "Inventory must contain exactly one item for each discoverable path.",
                    path,
                )
            )
        owners = path_index.get(path, [])
        if len(owners) == 0:
            issues.append(
                _ledger_error(
                    "source_missing_from_ledger",
                    "Discoverable raw source has no ledger record.",
                    path,
                )
            )
    for diagnostic in sorted(
        report.skipped,
        key=lambda item: (
            "" if item.path is None else item.path.as_posix(),
            item.code,
            item.message,
        ),
    ):
        issues.append(
            _ledger_error(
                diagnostic.code,
                diagnostic.message,
                diagnostic.path,
                dict(diagnostic.details),
            )
        )

    sentinel = False
    summary_readable = True
    if summary_bytes is None:
        try:
            summary = LedgerStore(paths).read_summary()
        except (FileNotFoundError, OSError, ValueError):
            summary_readable = False
            issues.append(
                _ledger_error(
                    "ledger_summary_unreadable",
                    "sources/ledger.md could not be read safely.",
                    PurePosixPath("sources/ledger.md"),
                )
            )
            summary = b""
    elif type(summary_bytes) is bytes:
        summary = summary_bytes
    else:
        raise ValueError("summary_bytes must be bytes or None")
    if not summary_readable:
        pass
    elif summary == _PRISTINE_LEDGER:
        sentinel = True
        if valid_records or report.items or report.skipped:
            issues.append(
                _ledger_error(
                    "ledger_not_initialized_with_sources",
                    "The pre-initialization ledger sentinel is only valid for a pristine repository.",
                    PurePosixPath("sources/ledger.md"),
                )
            )
    elif not summary:
        issues.append(
            _ledger_error(
                "ledger_summary_mismatch",
                "Ledger summary is empty instead of a canonical initialized or pristine summary.",
                PurePosixPath("sources/ledger.md"),
            )
        )
    else:
        summary_issue = _validate_summary_equivalence(
            paths, summary, valid_records.values()
        )
        if summary_issue is not None:
            issues.append(summary_issue)

    for record in valid_records.values():
        raw_origins = _validated_raw_origins(record, issues)
        if record.current_raw_path not in inventory_paths:
            issues.append(
                _ledger_error(
                    "ledgered_raw_path_missing",
                    "Ledgered current raw path is not discoverable.",
                    record.current_raw_path,
                    {"source_id": record.source_id},
                )
            )
        else:
            item = inventory_items[record.current_raw_path]
            descriptor = item.url_descriptor
            expected_descriptor = record.url_descriptor
            if expected_descriptor is not None and (
                descriptor is None
                or (
                    record.active_content_sha256 is None
                    and (
                        record.media_type != item.media_type
                        or record.byte_size != item.fingerprint.byte_size
                    )
                )
                or expected_descriptor.fingerprint != item.fingerprint
                or expected_descriptor.url != descriptor.url
                or expected_descriptor.description != descriptor.description
                or expected_descriptor.added != descriptor.added
            ):
                issues.append(
                    _ledger_error(
                        "url_descriptor_mismatch",
                        "Live URL descriptor metadata does not match its ledger record.",
                        record.current_raw_path,
                    )
                )
            elif expected_descriptor is None and (
                descriptor is not None or record.media_type != item.media_type
            ):
                issues.append(
                    _ledger_error(
                        "raw_inventory_metadata_mismatch",
                        "Live raw source type does not match its ledger record.",
                        record.current_raw_path,
                    )
                )
        if record.active_content_sha256 is not None:
            active_version = record.versions[record.active_content_sha256]
            metadata_matches = record.byte_size == active_version.byte_size
            if record.url_descriptor is None:
                metadata_matches = (
                    metadata_matches
                    and record.current_raw_path == active_version.raw_path
                )
            else:
                metadata_matches = (
                    metadata_matches
                    and bool(active_version.retrieval_events)
                    and record.media_type
                    == active_version.retrieval_events[-1].detected_media_type
                    and active_version.byte_size
                    == active_version.retrieval_events[-1].byte_size
                )
            if not metadata_matches:
                issues.append(
                    _ledger_error(
                        "active_version_metadata_mismatch",
                        "Record metadata does not match its active content version.",
                        record.current_raw_path,
                    )
                )
        if record.state is SourceState.EXTRACTING:
            issues.append(
                _ledger_error(
                    "stale_extracting_state",
                    "Ledger contains an interrupted extracting checkpoint.",
                    record.current_raw_path,
                )
            )
        if (
            record.state in {SourceState.OK, SourceState.WARNING}
            and record.active_derivation_id is None
        ):
            issues.append(
                _ledger_error(
                    "active_representation_missing",
                    "Searchable source state requires an active representation.",
                    record.current_raw_path,
                    {"source_id": record.source_id},
                )
            )
        if record.active_derivation_id is not None:
            active = record.derivations[record.active_derivation_id]
            operational_warning = record.state is SourceState.WARNING and any(
                diagnostic.code in _SOURCE_OPERATIONAL_WARNING_CODES
                for diagnostic in record.diagnostics
            )
            quality_mismatch = (
                record.state is SourceState.OK and active.quality_state != "ok"
            ) or (
                record.state is SourceState.WARNING
                and not operational_warning
                and active.quality_state != "warning"
            )
            if quality_mismatch:
                issues.append(
                    _ledger_error(
                        "active_quality_state_mismatch",
                        "Searchable record state does not match active derivation quality.",
                        active.output_path,
                    )
                )
        for checksum, version in sorted(record.versions.items()):
            if not _raw_path_has_record_role(record, checksum, version.raw_path):
                issues.append(
                    _ledger_error(
                        "raw_path_role_invalid",
                        "Retained raw materialization path does not match its record, checksum, and active role.",
                        version.raw_path,
                        {"source_id": record.source_id, "sha256": checksum},
                    )
                )
                continue
            namespace = _raw_namespace(version.raw_path)
            snapshot = _observe_or_issue(
                cache,
                paths,
                namespace,
                version.raw_path,
                full=full,
                issues=issues,
                unavailable_code="raw_file_unavailable",
                details={"source_id": record.source_id, "sha256": checksum},
            )
            if snapshot is None:
                continue
            if snapshot.byte_size != version.byte_size:
                issues.append(
                    _ledger_error(
                        "raw_size_mismatch",
                        "Raw materialization size does not match the retained version.",
                        version.raw_path,
                    )
                )
            if snapshot.mtime_ns != version.fingerprint.mtime_ns:
                issues.append(
                    _ledger_error(
                        "raw_fingerprint_mismatch",
                        "Raw materialization timestamp does not match the retained fingerprint.",
                        version.raw_path,
                    )
                )
            if full and snapshot.sha256 != checksum:
                issues.append(
                    _ledger_error(
                        "raw_checksum_mismatch",
                        "Raw materialization checksum does not match the retained version.",
                        version.raw_path,
                    )
                )

        for identifier, derivation in sorted(record.derivations.items()):
            provenance_valid = _validate_derivation_provenance(
                identifier, derivation, issues
            )
            if derivation.source_sha256 not in record.versions:
                issues.append(
                    _ledger_error(
                        "derivation_version_link_invalid",
                        "Retained derivation does not link to a retained content version.",
                        derivation.output_path,
                    )
                )
                provenance_valid = False
            path_valid = _canonical_derivation_output_structure(
                record, derivation, raw_origins
            )
            if not path_valid:
                issues.append(
                    _ledger_error(
                        "derivation_output_path_invalid",
                        "Retained derivation output path does not match its stored identity.",
                        derivation.output_path,
                    )
                )
            if not provenance_valid or not path_valid:
                continue
            snapshot = _observe_or_issue(
                cache,
                paths,
                SnapshotNamespace.EXTRACTED,
                derivation.output_path,
                full=full,
                issues=issues,
                unavailable_code="derivation_output_unavailable",
                details={
                    "source_id": record.source_id,
                    "derivation_id": identifier,
                },
            )
            if snapshot is None:
                continue
            if snapshot.byte_size != derivation.output_byte_size:
                issues.append(
                    _ledger_error(
                        "output_size_mismatch",
                        "Extracted output size does not match the retained derivation.",
                        derivation.output_path,
                    )
                )
            if snapshot.mtime_ns != derivation.output_mtime_ns:
                issues.append(
                    _ledger_error(
                        "output_fingerprint_mismatch",
                        "Extracted output timestamp does not match the retained derivation.",
                        derivation.output_path,
                    )
                )
            if full and snapshot.sha256 != derivation.output_sha256:
                issues.append(
                    _ledger_error(
                        "output_checksum_mismatch",
                        "Extracted output checksum does not match the retained derivation.",
                        derivation.output_path,
                    )
                )

    if full:
        # Handoffs are historical evidence; current registry equality is checked
        # only when registering new agent output.
        from .extractors.handoff import validate_handoff_manifests

        issues.extend(validate_handoff_manifests(paths, valid_records))
    issues.sort(key=_issue_key)
    revision = (
        None
        if sentinel and not valid_records
        else compute_corpus_revision(valid_records.values())
    )
    return ValidationReport(("source-ledger",), tuple(issues), revision)


def _observe_or_issue(
    cache: ChecksumCache,
    paths: RepoPaths,
    namespace: SnapshotNamespace,
    logical_path: PurePosixPath,
    *,
    full: bool,
    issues: list[ValidationIssue],
    unavailable_code: str,
    details: dict[str, object],
) -> StableFileSnapshot | None:
    try:
        return cache.observe(paths, namespace, logical_path, full=full)
    except (InventoryAccessError, OSError, ValueError) as error:
        issues.append(
            _ledger_error(
                unavailable_code,
                f"Ledgered file could not be observed safely: {error}",
                logical_path,
                details,
            )
        )
        return None


def _raw_namespace(path: PurePosixPath) -> SnapshotNamespace:
    if path.parts[:1] == ("_versions",):
        return SnapshotNamespace.RAW_VERSION
    if path.parts[:1] == ("_web",):
        return SnapshotNamespace.RAW_WEB
    return SnapshotNamespace.RAW_USER


def _raw_path_has_record_role(
    record: SourceRecord, checksum: str, raw_path: PurePosixPath
) -> bool:
    parts = raw_path.parts
    if record.url_descriptor is not None:
        return len(parts) == 4 and parts[:3] == ("_web", record.source_id, checksum)
    if checksum == record.active_content_sha256:
        return raw_path == record.current_raw_path and parts[:1] not in {
            ("_versions",),
            ("_web",),
        }
    return len(parts) == 4 and parts[:3] == ("_versions", record.source_id, checksum)


def _canonical_derivation_output_structure(
    record: SourceRecord,
    derivation: object,
    raw_origins: frozenset[PurePosixPath],
) -> bool:
    output_path = getattr(derivation, "output_path")
    source_sha256 = getattr(derivation, "source_sha256")
    identifier = getattr(derivation, "derivation_id")
    embedded_origin = PurePosixPath(*output_path.parts[2:-2])
    source_version = record.versions.get(source_sha256)
    url_origin_matches_version = record.url_descriptor is None or (
        source_version is not None and embedded_origin == source_version.raw_path
    )
    return (
        output_path.parts[:2] == ("sources", "extracted")
        and len(output_path.parts) >= 5
        and embedded_origin in raw_origins
        and url_origin_matches_version
        and output_path.parts[-2] == source_sha256
        and output_path.name == identifier + ".md"
    )


def _validated_raw_origins(
    record: SourceRecord,
    issues: list[ValidationIssue],
) -> frozenset[PurePosixPath]:
    """Return output origins supported by the immutable evidence still retained."""

    valid: set[PurePosixPath] = set()
    is_url = record.url_descriptor is not None
    for path in (record.current_raw_path, *record.previous_raw_paths):
        try:
            validate_inventory_path(path, "ledger raw origin")
            suffix_matches = is_url_descriptor_path(path) == is_url
        except ValueError:
            suffix_matches = False
        if not suffix_matches:
            issues.append(
                _ledger_error(
                    "raw_origin_path_invalid",
                    "Ledger raw origin does not have a canonical source-path role.",
                    path,
                    {"source_id": record.source_id},
                )
            )
            continue
        valid.add(path)

    creation_path = (
        record.previous_raw_paths[0]
        if record.previous_raw_paths
        else record.current_raw_path
    )
    identity_matches = creation_path in valid
    if not is_url:
        root_checksum = (
            record.adoption_events[0].prior_sha256
            if record.adoption_events
            else next(iter(record.versions), "")
        )
        identity_matches = identity_matches and (
            source_id_for_first_seen(creation_path, root_checksum) == record.source_id
        )
    # A URL source ID also commits the first URL, but the present schema keeps
    # only the mutable current URL. Do not manufacture an identity proof from it.
    if not identity_matches:
        issues.append(
            _ledger_error(
                "raw_origin_identity_invalid",
                "Ledger raw origins are not bound to the source creation identity.",
                creation_path,
                {"source_id": record.source_id},
            )
        )
        return frozenset()
    if is_url:
        web_origins: set[PurePosixPath] = set()
        for checksum, version in record.versions.items():
            if _raw_path_has_record_role(record, checksum, version.raw_path):
                web_origins.add(version.raw_path)
        return frozenset(web_origins)
    return frozenset(valid)


def _validate_derivation_provenance(
    identifier: str,
    derivation: object,
    issues: list[ValidationIssue],
) -> bool:
    """Validate immutable present-schema relations without today's registry."""

    output_path = getattr(derivation, "output_path")
    extractor_id = getattr(derivation, "extractor_id")
    extractor_version = getattr(derivation, "extractor_version")
    method = getattr(derivation, "method")
    method_metadata = getattr(derivation, "method_metadata")
    valid = True

    if not is_normalized_identifier(extractor_id):
        issues.append(
            _ledger_error(
                "derivation_extractor_id_invalid",
                "Retained derivation extractor ID is not canonical.",
                output_path,
            )
        )
        valid = False

    base_version, separator, prerequisite_digest = extractor_version.rpartition("+")
    version_valid = (
        separator == "+"
        and bool(base_version)
        and "\0" not in base_version
        and _SHA256_RE.fullmatch(prerequisite_digest) is not None
    )
    if not version_valid:
        issues.append(
            _ledger_error(
                "derivation_extractor_version_invalid",
                "Retained derivation extractor version is not canonical.",
                output_path,
            )
        )
        valid = False

    if method == "deterministic":
        converter_id = method_metadata.get("converter_id")
        converter_version = method_metadata.get("converter_version")
        committed_builtin = is_committed_builtin_converter_id(converter_id)
        reserved_builtin = type(converter_id) is str and converter_id.startswith(
            "builtin."
        )
        converter_id_valid = is_normalized_identifier(converter_id) and not (
            reserved_builtin and not committed_builtin
        )
        if not converter_id_valid:
            issues.append(
                _ledger_error(
                    "derivation_converter_id_invalid",
                    "Retained derivation converter ID is not canonical.",
                    output_path,
                )
            )
            valid = False
        converter_version_valid = not (
            type(converter_version) is not str
            or not converter_version.strip()
            or "\0" in converter_version
        )
        if converter_version_valid and committed_builtin:
            converter_version_valid = (
                version_valid
                and converter_version == f"builtin:{converter_id}:{base_version}"
            )
        elif converter_version_valid and not reserved_builtin:
            converter_version_valid = converter_version == " ".join(
                converter_version.split()
            )
        if not converter_version_valid:
            issues.append(
                _ledger_error(
                    "derivation_converter_version_invalid",
                    "Retained derivation converter version is not canonical.",
                    output_path,
                )
            )
            valid = False
        if (
            version_valid
            and converter_id_valid
            and converter_version_valid
            and type(converter_version) is str
        ):
            observed_digest = hashlib.sha256(
                f"converter-v1\0{converter_id}\0{converter_version}".encode("utf-8")
            ).hexdigest()
            if observed_digest != prerequisite_digest:
                issues.append(
                    _ledger_error(
                        "derivation_prerequisite_mismatch",
                        "Retained derivation converter is not bound to its prerequisite digest.",
                        output_path,
                    )
                )
                valid = False
    elif any(
        type(value) is not str or not value.strip() or "\0" in value
        for value in method_metadata.values()
    ):
        issues.append(
            _ledger_error(
                "derivation_method_metadata_invalid",
                "Retained derivation method metadata is not canonical.",
                output_path,
            )
        )
        valid = False

    if valid:
        expected_identifier = derivation_id(
            source_sha256=getattr(derivation, "source_sha256"),
            extractor_id=extractor_id,
            extractor_version=extractor_version,
            config_sha256=getattr(derivation, "config_sha256"),
        )
        if identifier != expected_identifier:
            issues.append(
                _ledger_error(
                    "derivation_identity_mismatch",
                    "Retained derivation ID does not match its identity fields.",
                    output_path,
                )
            )
            valid = False
    return valid


def _validate_summary_equivalence(
    paths: RepoPaths,
    summary: bytes,
    records: object,
) -> ValidationIssue | None:
    lines = summary.splitlines()
    timestamp_matches = [
        match
        for line in lines
        if (match := _SUMMARY_TIMESTAMP_RE.fullmatch(line)) is not None
    ]
    if len(timestamp_matches) != 1:
        return _ledger_error(
            "ledger_summary_mismatch",
            "Ledger summary must contain exactly one canonical synchronization timestamp.",
            PurePosixPath("sources/ledger.md"),
        )
    timestamp_text = timestamp_matches[0].group(1).decode("ascii")
    try:
        generated_at = datetime.fromisoformat(timestamp_text[:-1] + "+00:00")
    except ValueError:
        return _ledger_error(
            "ledger_summary_mismatch",
            "Ledger summary synchronization timestamp is invalid.",
            PurePosixPath("sources/ledger.md"),
        )
    if (
        generated_at.tzinfo is None
        or generated_at.astimezone(timezone.utc) != generated_at
    ):
        return _ledger_error(
            "ledger_summary_mismatch",
            "Ledger summary synchronization timestamp is not UTC.",
            PurePosixPath("sources/ledger.md"),
        )
    try:
        expected = (
            LedgerStore(paths)
            .render_summary(
                records,
                generated_at=generated_at,  # type: ignore[arg-type]
            )
            .encode("utf-8")
        )
    except (TypeError, ValueError):
        return _ledger_error(
            "ledger_summary_mismatch",
            "Ledger summary could not be reconstructed from the retained records.",
            PurePosixPath("sources/ledger.md"),
        )
    if expected != summary:
        return _ledger_error(
            "ledger_summary_mismatch",
            "Ledger summary does not match the canonical record snapshot.",
            PurePosixPath("sources/ledger.md"),
        )
    return None


def _ledger_error(
    code: str,
    message: str,
    path: PurePosixPath | None = None,
    details: Mapping[str, object] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        "error",
        code,
        message,
        path,
        {} if details is None else details,  # type: ignore[arg-type]
    )


def _issue_key(issue: ValidationIssue) -> tuple[str, str, str, str]:
    return (
        "" if issue.path is None else issue.path.as_posix(),
        issue.code,
        issue.message,
        repr(sorted(issue.details.items())),
    )


def validate_template_layout(paths: RepoPaths) -> ValidationReport:
    issues: list[ValidationIssue] = []
    launcher = paths.root / "brain"
    if not launcher.is_file() or not launcher.stat().st_mode & stat.S_IXUSR:
        issues.append(
            _error(
                "launcher_invalid",
                "brain must be a file executable by its owner.",
                PurePosixPath("brain"),
            )
        )

    claude_entrypoint = paths.root / "CLAUDE.md"
    if (
        not claude_entrypoint.is_symlink()
        or claude_entrypoint.readlink() != PurePosixPath("AGENTS.md")
        or claude_entrypoint.resolve() != paths.root / "AGENTS.md"
    ):
        issues.append(
            _error(
                "claude_entrypoint_invalid",
                "CLAUDE.md must be the relative symlink AGENTS.md.",
                PurePosixPath("CLAUDE.md"),
            )
        )

    for relative in _REQUIRED_DIRECTORIES:
        if not (paths.root / relative).is_dir():
            issues.append(
                _error(
                    "required_directory_missing",
                    "Required repository directory is missing.",
                    relative,
                )
            )

    for relative in _REQUIRED_SCHEMAS:
        if not (paths.root / relative).is_file():
            issues.append(
                _error(
                    "required_schema_missing",
                    "Required versioned schema is missing.",
                    relative,
                )
            )

    wiki_index = PurePosixPath("wiki/index.md")
    if not (paths.root / wiki_index).is_file():
        issues.append(
            _error(
                "wiki_index_missing",
                "wiki/index.md is missing.",
                wiki_index,
            )
        )

    return ValidationReport(
        checks=("template-layout",),
        issues=tuple(issues),
        corpus_revision=None,
    )


def merge_reports(*reports: ValidationReport) -> ValidationReport:
    checks: list[str] = []
    seen_checks: set[str] = set()
    issues: list[ValidationIssue] = []
    revisions: list[str] = []

    for report in reports:
        for check in report.checks:
            if check not in seen_checks:
                seen_checks.add(check)
                checks.append(check)
        issues.extend(report.issues)
        if (
            report.corpus_revision is not None
            and report.corpus_revision not in revisions
        ):
            revisions.append(report.corpus_revision)

    corpus_revision = revisions[0] if len(revisions) == 1 else None
    if len(revisions) > 1:
        issues.append(
            ValidationIssue(
                "error",
                "corpus_revision_conflict",
                "Validation reports contain conflicting corpus revisions.",
            )
        )

    return ValidationReport(tuple(checks), tuple(issues), corpus_revision)


def wiki_documents(paths: RepoPaths) -> tuple[Path, ...]:
    """Return only direct, regular logical wiki records in canonical order."""

    documents: list[Path] = []
    for directory, prefix in (
        (paths.wiki_pages, ("wiki", "pages")),
        (paths.wiki_questions, ("wiki", "questions")),
    ):
        if directory != paths.root / "/".join(prefix):
            raise ValueError("wiki roots must use canonical repository paths")
        with _PinnedDirectory.open(directory) as pinned:
            for name in sorted(os.listdir(pinned.descriptor)):
                observed = os.stat(
                    name, dir_fd=pinned.descriptor, follow_symlinks=False
                )
                if not name.endswith(".md"):
                    continue
                if not stat.S_ISREG(observed.st_mode):
                    raise ValueError("wiki record tree contains an unsafe Markdown entry")
                documents.append(directory / name)
            pinned.validate()
    return tuple(sorted(documents, key=lambda item: item.as_posix()))


def validate_wiki(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    full: bool,
    checksum_cache: ChecksumCache | None = None,
) -> ValidationReport:
    """Validate current direct wiki records and their graph as one snapshot."""

    from .citations import validate_citations
    from .graph import _load_live_documents, validate_graph

    # `_load_live_documents` reads direct regular records through pinned
    # directory descriptors; keep those captured text values authoritative for
    # both citation and graph validation rather than following names again.
    try:
        expected_paths = wiki_documents(paths)
        loaded = _load_live_documents(paths)
        documents = {path: text for path, text in loaded.values()}
        if tuple(sorted(documents, key=lambda item: item.as_posix())) != expected_paths:
            raise ValueError("wiki record mapping changed while validation was captured")
    except (OSError, UnicodeError, ValueError) as error:
        return ValidationReport(
            ("wiki-documents",),
            (
                ValidationIssue(
                    "error",
                    "wiki_documents_invalid",
                    f"Logical wiki documents cannot be read safely: {error}",
                ),
            ),
            None,
        )
    citation_report = validate_citations(
        paths, ledger, documents, full=full, checksum_cache=checksum_cache
    )
    interpretation_issues: list[ValidationIssue] = []
    if citation_report.ok:
        from .wiki_interpretations import validate_interpretation_document
        from .wiki_models import parse_question

        for path, text in sorted(documents.items(), key=lambda item: item[0].as_posix()):
            if path.parent == paths.wiki_questions:
                try:
                    record = parse_question(path, text=text)
                except ValueError:
                    # Graph validation reports record-model failures with its
                    # established diagnostic contract.
                    continue
                interpretation_issues.extend(
                    validate_interpretation_document(path, text, record)
                )
    graph_report = validate_graph(
        paths,
        documents=documents,
        corpus_revision=citation_report.corpus_revision,
    )
    interpretation_report = ValidationReport(
        ("wiki-interpretations",),
        tuple(interpretation_issues),
        citation_report.corpus_revision,
    )
    return merge_reports(citation_report, interpretation_report, graph_report)


def validate_repository(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    full: bool,
    checksum_cache: ChecksumCache | None = None,
) -> tuple[ValidationReport, ...]:
    """Run final repository validation under one source-then-wiki snapshot."""

    from .validators import validate_instruction_architecture
    from .wiki_transaction import validate_wiki_transaction_state

    with SourceWriteLock.acquire(paths.lock):
        with SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock"):
            layout_report = validate_template_layout(paths)
            instruction_report = validate_instruction_architecture(paths)
            try:
                records = ledger.load_all()
            except (OSError, ValueError) as error:
                return (
                    layout_report,
                    ValidationReport(
                        ("source-ledger",),
                        (
                            ValidationIssue(
                                "error",
                                "ledger_records_invalid",
                                f"Source ledger cannot be read safely: {error}",
                            ),
                        ),
                        None,
                    ),
                    instruction_report,
                )
            cache = (checksum_cache or ChecksumCache()) if full else checksum_cache
            if full:
                assert cache is not None
                cache.begin_transaction()
            revision = compute_corpus_revision(records.values())
            source_report = validate_source_ledger(
                paths, records, full=full, checksum_cache=cache
            )
            transaction_report = validate_wiki_transaction_state(
                paths, corpus_revision=revision
            )
            if not transaction_report.ok:
                return (
                    layout_report,
                    source_report,
                    transaction_report,
                    instruction_report,
                )
            return (
                layout_report,
                source_report,
                transaction_report,
                validate_wiki(paths, ledger, full=full, checksum_cache=cache),
                instruction_report,
            )


def _error(code: str, message: str, path: PurePosixPath) -> ValidationIssue:
    return ValidationIssue("error", code, message, path)
