from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .commands import (
    DEFAULT_SERVICES,
    CommandServices,
    acknowledge_sync_result_id,
    adopt_source_version,
    consume_sync_result_id,
    doctor,
    init_sources,
    register_source_extraction,
    snapshot_source_url,
    status,
    sync_sources,
    unavailable,
    validate,
)
from .output import CommandResult, render_human, render_json
from .diagnostics import Diagnostic
from .layout import RepoPaths
from .ledger import LedgerStore
from .sources.web import ApprovalClaim


class _ParserError(Exception):
    pass


class _HelpRequested(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


class _HelpAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        raise _HelpRequested(parser)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.add_argument(
            "-h",
            "--help",
            action=_HelpAction,
            nargs=0,
            help="show this help message and exit",
        )

    def error(self, message: str) -> None:
        raise _ParserError(message)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{64}$")
_RESULT_ID_RE = re.compile(r"^sync_[0-9a-f]{64}$")


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    services: CommandServices | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    parser = _build_parser()
    try:
        namespace = parser.parse_args(arguments)
    except _HelpRequested as requested:
        requested.parser.print_help(output)
        return 0
    except _ParserError as error:
        command = _json_error_command(arguments)
        if command is not None:
            render_json(
                CommandResult(
                    command,
                    False,
                    {},
                    errors=(Diagnostic("invalid_arguments", str(error)),),
                ),
                output,
            )
            output.flush()
            return 2
        errors.write(f"{parser.prog}: error: {error}\n")
        return 2

    if namespace.command is None:
        errors.write(f"{parser.prog}: error: a command is required\n")
        return 2

    workdir = Path.cwd() if cwd is None else cwd
    result = _dispatch(
        namespace,
        workdir,
        DEFAULT_SERVICES if services is None else services,
    )
    if namespace.json:
        render_json(result, output)
        output.flush()
    elif result.command == "search" and result.data:
        data = result.data
        output.write(
            f"search: run {data['run_id']} page {data['page_index']}; "
            f"{data['candidate_count']} candidates; {len(data['matches'])} returned records\n"
        )
        if result.errors:
            for diagnostic in result.errors:
                errors.write(f"{diagnostic.code}: {diagnostic.message}\n")
        elif data["next_cursor"]:
            output.write(f"Continue: ./brain search --cursor {data['next_cursor']}\n")
        else:
            output.write("complete\n")
        output.flush()
        errors.flush()
    else:
        render_human(result, stdout=output, stderr=errors)
        output.flush()
        errors.flush()
    return exit_code_for(result)


def _dispatch(
    namespace: argparse.Namespace,
    cwd: Path,
    services: CommandServices,
) -> CommandResult:
    command = namespace.command
    if command == "doctor":
        return doctor(cwd)
    if command == "init":
        return init_sources(cwd, services=services, max_workers=namespace.max_workers)
    if command == "sync":
        return sync_sources(cwd, services=services, max_workers=namespace.max_workers)
    if command == "status":
        return status(cwd)
    if command == "search":
        return _search_command(namespace, cwd)
    if command == "links candidates":
        return _links_candidates_command(namespace, cwd)
    if command == "links check":
        return _links_check_command(cwd)
    if command == "wiki apply":
        return _wiki_apply_command(namespace, cwd)
    if command == "wiki recover":
        return _wiki_recover_command(cwd)
    if command == "validate":
        return validate(cwd, full=namespace.full)
    if command == "source adopt-version":
        return adopt_source_version(
            cwd,
            namespace.source_id,
            candidate_sha256=namespace.candidate_sha256,
            approval_note=namespace.approval_note,
        )
    if command == "source acknowledge-sync-result":
        return acknowledge_sync_result_id(cwd, namespace.result_id)
    if command == "source consume-sync-result":
        return consume_sync_result_id(cwd, namespace.result_id)
    if command == "source register-extraction":
        return register_source_extraction(
            cwd,
            handoff_id=namespace.handoff_id,
            staging_path=namespace.staging_path,
            anchors_json=namespace.anchors_json,
            quality_state=namespace.quality_state,
            note=namespace.note,
            services=services,
        )
    if command == "source snapshot-url":
        return snapshot_source_url(
            cwd,
            source_id=namespace.source_id,
            url=namespace.url,
            description=namespace.description,
            approval=ApprovalClaim(
                namespace.approval_event_id,
                namespace.approval_scope,
                namespace.approval_note,
            ),
            rendered_staging_path=namespace.rendered_staging_path,
            handoff_id=namespace.handoff_id,
            retrieved_at=namespace.retrieved_at,
            final_url=namespace.final_url,
            redirect_urls=tuple(namespace.redirect_url),
            detected_media_type=namespace.detected_media_type,
            services=services,
        )
    assert namespace.milestone is not None
    return unavailable(command, namespace.milestone)


def exit_code_for(result: CommandResult) -> int:
    if result.ok:
        return 0
    if any(diagnostic.code == "command_not_available" for diagnostic in result.errors):
        return 64
    if any(
        diagnostic.code
        in {
            "links_check_failed",
            "links_candidates_failed",
            "wiki_transaction_failed",
            "wiki_transaction_locked",
        }
        for diagnostic in result.errors
    ):
        return 2
    if result.command == "validate" and any(
        diagnostic.code in {"repository_not_found", "source_operation_failed"}
        for diagnostic in result.errors
    ):
        return 2
    if result.command in {"search", "links candidates", "wiki apply", "wiki recover"} and any(
        diagnostic.code
        in {
            "invalid_arguments",
            "invalid_search_cursor",
            "rg_unavailable",
            "rg_failed",
            "search_argument_limit",
            "search_operand_invalid",
            "search_output_limit",
            "search_failed",
            "invalid_manifest",
        }
        for diagnostic in result.errors
    ):
        return 2
    return 1


def _search_command(namespace: argparse.Namespace, cwd: Path) -> CommandResult:
    from dataclasses import fields
    from pathlib import PurePath
    from .locking import LockHeldError, LockCleanupError
    from .search import (
        SearchError,
        SearchRequest,
        SearchRunBlocked,
        cleanup_search_runs,
        resume_search,
        search_active_sources,
        search_wiki,
    )

    def encode(value):
        if isinstance(value, PurePath):
            return value.as_posix()
        if isinstance(value, tuple):
            return [encode(item) for item in value]
        if isinstance(value, (Diagnostic,)) or hasattr(value, "__dataclass_fields__"):
            return {
                field.name: encode(getattr(value, field.name))
                for field in fields(value)
            }
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        return value

    try:
        start_values = (
            namespace.scope,
            namespace.pass_name,
            namespace.term,
            namespace.context,
            namespace.page_size,
            namespace.max_run_bytes,
            namespace.freshness,
            namespace.source_id,
        )
        if namespace.cursor is not None:
            if any(value is not None for value in start_values):
                raise ValueError("--cursor cannot be combined with any start flag")
            request = None
        else:
            if bool(namespace.freshness) != bool(namespace.source_id):
                raise ValueError(
                    "--freshness requires --source-id; --source-id requires --freshness"
                )
            request = SearchRequest(
                namespace.scope,
                namespace.pass_name,
                tuple(namespace.term or ()),
                2 if namespace.context is None else namespace.context,
                100 if namespace.page_size is None else namespace.page_size,
                1_073_741_824
                if namespace.max_run_bytes is None
                else namespace.max_run_bytes,
                tuple(namespace.source_id or ()),
            )
        paths = RepoPaths.discover(cwd)
        ledger = LedgerStore(paths)
        cleanup_search_runs(paths)
        if request is None:
            result = resume_search(paths, ledger, namespace.cursor)
        else:
            search = (
                search_active_sources if request.scope == "sources" else search_wiki
            )
            result = search(paths, ledger, request)
        return CommandResult(
            "search",
            not result.coverage_gaps,
            encode(result),
            errors=result.coverage_gaps,
        )
    except SearchRunBlocked as error:
        return CommandResult("search", False, {}, errors=(error.diagnostic,))
    except SearchError as error:
        return CommandResult(
            "search", False, {}, errors=(Diagnostic(error.code, str(error)),)
        )
    except (LockHeldError, LockCleanupError) as error:
        return CommandResult(
            "search",
            False,
            {},
            errors=(Diagnostic("search_run_incomplete", str(error)),),
        )
    except (ValueError, OSError) as error:
        return CommandResult(
            "search", False, {}, errors=(Diagnostic("invalid_arguments", str(error)),)
        )


def _links_candidates_command(namespace: argparse.Namespace, cwd: Path) -> CommandResult:
    from dataclasses import fields
    from pathlib import PurePath

    from .graph import find_link_candidates, resume_link_candidates
    from .locking import LockCleanupError, LockHeldError
    from .search import (
        InvalidSearchCursor,
        SearchError,
        SearchOperandError,
        SearchRunBlocked,
        canonical_wiki_logical_path,
        cleanup_search_runs,
    )

    def encode(value):
        if isinstance(value, PurePath):
            return value.as_posix()
        if isinstance(value, tuple):
            return [encode(item) for item in value]
        if isinstance(value, (Diagnostic,)) or hasattr(value, "__dataclass_fields__"):
            return {
                field.name: encode(getattr(value, field.name))
                for field in fields(value)
            }
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        return value

    try:
        paths = RepoPaths.discover(cwd)
        ledger = LedgerStore(paths)
        if namespace.cursor is not None:
            if any(
                value is not None
                for value in (
                    namespace.page_path,
                    namespace.term,
                    namespace.page_size,
                    namespace.max_run_bytes,
                )
            ):
                raise ValueError("--cursor cannot be combined with start arguments")
            page_path = None
        else:
            if namespace.page_path is None or not namespace.term:
                raise ValueError("a page path and at least one --term are required")
            logical = canonical_wiki_logical_path(
                paths, namespace.page_path, allow_absent=True
            )
            page_path = paths.root / logical
        cleanup_search_runs(paths)
        result = (
            resume_link_candidates(paths, ledger, namespace.cursor)
            if page_path is None
            else find_link_candidates(
                paths,
                ledger,
                page_path=page_path,
                terms=tuple(namespace.term),
                page_size=100 if namespace.page_size is None else namespace.page_size,
                max_run_bytes=(
                    1_073_741_824
                    if namespace.max_run_bytes is None
                    else namespace.max_run_bytes
                ),
            )
        )
        return CommandResult(
            "links candidates",
            not result.coverage_gaps,
            encode(result),
            errors=result.coverage_gaps,
        )
    except InvalidSearchCursor as error:
        return CommandResult(
            "links candidates",
            False,
            {},
            errors=(Diagnostic("invalid_search_cursor", str(error)),),
        )
    except SearchRunBlocked as error:
        return CommandResult("links candidates", False, {}, errors=(error.diagnostic,))
    except SearchOperandError as error:
        return CommandResult(
            "links candidates",
            False,
            {},
            errors=(Diagnostic("invalid_arguments", str(error)),),
        )
    except SearchError as error:
        return CommandResult(
            "links candidates", False, {}, errors=(Diagnostic(error.code, str(error)),)
        )
    except (LockHeldError, LockCleanupError) as error:
        return CommandResult(
            "links candidates",
            False,
            {},
            errors=(Diagnostic("links_candidates_failed", str(error)),),
        )
    except (ValueError, OSError) as error:
        return CommandResult(
            "links candidates",
            False,
            {},
            errors=(Diagnostic("invalid_arguments", str(error)),),
        )


def _links_check_command(cwd: Path) -> CommandResult:
    from .graph import validate_graph

    try:
        paths = RepoPaths.discover(cwd)
        report = validate_graph(paths)
        warnings = tuple(
            Diagnostic(issue.code, issue.message, issue.path, issue.details)
            for issue in report.issues
            if issue.severity == "warning"
        )
        errors = tuple(
            Diagnostic(issue.code, issue.message, issue.path, issue.details)
            for issue in report.issues
            if issue.severity == "error"
        )
        return CommandResult(
            "links check",
            report.ok,
            {"report": _validation_report_data(report)},
            warnings=warnings,
            errors=errors,
        )
    except (OSError, ValueError) as error:
        return CommandResult(
            "links check",
            False,
            {},
            errors=(Diagnostic("links_check_failed", str(error)),),
        )


def _wiki_apply_command(namespace: argparse.Namespace, cwd: Path) -> CommandResult:
    from .locking import LockCleanupError, LockHeldError
    from .wiki_transaction import (
        ApprovalRequired,
        CorpusRevisionChanged,
        InvalidWikiJournal,
        LinkCandidateCoverageError,
        WikiPreflightError,
        WikiTransactionError,
        apply_wiki_manifest,
        load_wiki_manifest,
    )

    try:
        paths = RepoPaths.discover(cwd)
    except OSError as error:
        return _wiki_error_result("wiki apply", "wiki_transaction_failed", error)
    try:
        manifest = load_wiki_manifest(
            paths, _manifest_operand(paths, namespace.manifest)
        )
    except (OSError, ValueError) as error:
        return _invalid_manifest_result("wiki apply", error)
    try:
        result = apply_wiki_manifest(paths, LedgerStore(paths), manifest)
        return CommandResult(
            "wiki apply",
            True,
            {
                "corpus_revision": result.corpus_revision,
                "changed_paths": sorted(path.as_posix() for path in result.changed_paths),
                "index_path": result.index_path.as_posix(),
                "recovered": result.recovered,
            },
        )
    except ApprovalRequired as error:
        return _wiki_error_result("wiki apply", "wiki_approval_required", error)
    except CorpusRevisionChanged as error:
        return _wiki_error_result("wiki apply", "wiki_corpus_revision_changed", error)
    except LinkCandidateCoverageError as error:
        return CommandResult("wiki apply", False, {}, errors=(error.diagnostic,))
    except WikiPreflightError as error:
        return _wiki_error_result("wiki apply", "wiki_preflight_failed", error)
    except InvalidWikiJournal as error:
        return _wiki_error_result("wiki apply", "wiki_journal_invalid", error)
    except (LockHeldError, LockCleanupError) as error:
        return _wiki_error_result("wiki apply", "wiki_transaction_locked", error)
    except ValueError as error:
        return _invalid_manifest_result("wiki apply", error)
    except (WikiTransactionError, OSError) as error:
        return _wiki_error_result("wiki apply", "wiki_transaction_failed", error)


def _wiki_recover_command(cwd: Path) -> CommandResult:
    from .locking import LockCleanupError, LockHeldError
    from .wiki_transaction import InvalidWikiJournal, WikiTransactionError, recover_wiki_transaction

    try:
        paths = RepoPaths.discover(cwd)
        result = recover_wiki_transaction(paths)
        return CommandResult(
            "wiki recover",
            True,
            {
                "recovered": result.recovered,
                "restored_paths": sorted(path.as_posix() for path in result.restored_paths),
            },
        )
    except InvalidWikiJournal as error:
        return _wiki_error_result("wiki recover", "wiki_journal_invalid", error)
    except (LockHeldError, LockCleanupError) as error:
        return _wiki_error_result("wiki recover", "wiki_transaction_locked", error)
    except (WikiTransactionError, OSError, ValueError) as error:
        return _wiki_error_result("wiki recover", "wiki_transaction_failed", error)


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="brain",
        description="Second Brain Lite command-line interface.",
        epilog=(
            "Public commands: doctor, init, sync, status, search, "
            "source snapshot-url, source register-extraction, source adopt-version, "
            "source consume-sync-result, source acknowledge-sync-result, "
            "wiki apply, wiki recover, links candidates, links check, validate --full."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the command result as JSON"
    )
    routes = parser.add_subparsers(dest="command")

    _add_route(routes, "doctor", command="doctor", help="inspect local prerequisites")
    for command in ("init", "sync"):
        _add_route(routes, command, command=command).add_argument(
            "--max-workers",
            type=_max_workers,
            default=None,
            help="maximum concurrent extraction jobs (1..16)",
        )
    _add_route(routes, "status", command="status")
    search = _add_route(
        routes,
        "search",
        command="search",
        help="search retained wiki or active source evidence",
    )
    search.add_argument("--scope", choices=("wiki", "sources"))
    search.add_argument(
        "--pass", dest="pass_name", choices=("discovery", "expansion", "verification")
    )
    search.add_argument("--term", action="append")
    search.add_argument("--context", type=int)
    search.add_argument("--page-size", type=int)
    search.add_argument("--max-run-bytes", type=int)
    search.add_argument("--freshness", action="store_true", default=None)
    search.add_argument("--source-id", action="append", type=_source_id)
    search.add_argument("--cursor")
    _add_route(routes, "validate", command="validate").add_argument(
        "--full", action="store_true", help="rehash all retained bytes"
    )

    source = routes.add_parser("source", help="source capture and provenance commands")
    source_routes = source.add_subparsers(dest="source_command", required=True)
    snapshot = _add_route(source_routes, "snapshot-url", command="source snapshot-url")
    target = snapshot.add_mutually_exclusive_group(required=True)
    target.add_argument("--source-id", type=_source_id)
    target.add_argument("--url")
    snapshot.add_argument("--description")
    snapshot.add_argument("--approval-event-id", required=True)
    snapshot.add_argument("--approval-scope", required=True)
    snapshot.add_argument("--approval-note", required=True)
    snapshot.add_argument("--rendered-staging-path", type=Path)
    snapshot.add_argument("--handoff-id", type=_handoff_id)
    snapshot.add_argument("--retrieved-at", type=_rfc3339_utc)
    snapshot.add_argument("--final-url")
    snapshot.add_argument("--redirect-url", action="append", default=[])
    snapshot.add_argument("--detected-media-type")
    registration = _add_route(
        source_routes,
        "register-extraction",
        command="source register-extraction",
    )
    registration.add_argument("--handoff-id", required=True, type=_handoff_id)
    registration.add_argument("--staging-path", required=True, type=Path)
    registration.add_argument("--anchors-json", required=True)
    registration.add_argument(
        "--quality-state", required=True, choices=("ok", "warning")
    )
    registration.add_argument("--note", required=True)
    adoption = _add_route(
        source_routes,
        "adopt-version",
        command="source adopt-version",
    )
    adoption.add_argument("source_id", type=_source_id)
    adoption.add_argument(
        "--candidate-sha256",
        required=True,
        type=_sha256,
    )
    adoption.add_argument(
        "--approval-note",
        required=True,
        type=_nonempty,
    )
    acknowledgement = _add_route(
        source_routes,
        "acknowledge-sync-result",
        command="source acknowledge-sync-result",
    )
    acknowledgement.add_argument(
        "--result-id",
        required=True,
        type=_result_id,
    )
    consumption = _add_route(
        source_routes,
        "consume-sync-result",
        command="source consume-sync-result",
    )
    consumption.add_argument(
        "--result-id",
        required=True,
        type=_result_id,
    )

    wiki = routes.add_parser("wiki", help="wiki transaction commands")
    wiki_routes = wiki.add_subparsers(dest="wiki_command", required=True)
    _add_route(wiki_routes, "apply", command="wiki apply").add_argument(
        "--manifest", required=True
    )
    _add_route(wiki_routes, "recover", command="wiki recover")

    links = routes.add_parser("links", help="wiki link commands")
    links_routes = links.add_subparsers(dest="links_command", required=True)
    candidates = _add_route(links_routes, "candidates", command="links candidates")
    candidates.add_argument("page_path", nargs="?")
    candidates.add_argument("--term", action="append")
    candidates.add_argument("--page-size", type=int)
    candidates.add_argument("--max-run-bytes", type=int)
    candidates.add_argument("--cursor")
    _add_route(links_routes, "check", command="links check")
    return parser


def _add_route(
    routes: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    *,
    command: str,
    milestone: int | None = None,
    help: str | None = None,
) -> argparse.ArgumentParser:
    route = routes.add_parser(name, help=help)
    route.set_defaults(command=command, milestone=milestone)
    if milestone is not None:
        route.add_argument(
            "arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
        )
    return route


def _max_workers(value: str) -> int:
    try:
        workers = int(value)
        if not 1 <= workers <= 16:
            raise ValueError
        return workers
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "max-workers must be an integer in 1..16"
        ) from error


def _rfc3339_utc(value: str) -> datetime:
    try:
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value
        ):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "retrieved-at must be an RFC3339 UTC timestamp"
        ) from error


def _sha256(value: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "checksum must be 64 lower-case hexadecimal characters"
        )
    return value


def _source_id(value: str) -> str:
    if _SOURCE_ID_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "source_id must be src_ followed by 64 lower-case hexadecimal characters"
        )
    return value


def _result_id(value: str) -> str:
    if _RESULT_ID_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "result_id must be sync_ followed by 64 lower-case hexadecimal characters"
        )
    return value


def _handoff_id(value: str) -> str:
    if re.fullmatch(r"hnd_[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "handoff_id must be hnd_ followed by 64 lower-case hexadecimal characters"
        )
    return value


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("approval note must be nonempty")
    return value


def _json_error_command(arguments: Sequence[str]) -> str | None:
    if len(arguments) < 2 or arguments[0] != "--json":
        return None
    command = arguments[1]
    if command in {"search", "validate"}:
        return command
    if command in {"wiki", "links"}:
        return command if len(arguments) < 3 else f"{command} {arguments[2]}"
    return None


def _validation_report_data(report) -> dict[str, object]:
    return {
        "checks": list(report.checks),
        "issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "message": issue.message,
                "path": None if issue.path is None else issue.path.as_posix(),
                "details": dict(issue.details),
            }
            for issue in report.issues
        ],
        "corpus_revision": report.corpus_revision,
    }


def _manifest_operand(paths: RepoPaths, value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError("wiki manifest must be a canonical repository-relative path")
    candidate = Path(value)
    if candidate.as_posix() != value or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("wiki manifest must be a canonical repository-relative path")
    return paths.root / candidate


def _invalid_manifest_result(command: str, error: Exception) -> CommandResult:
    return _wiki_error_result(command, "invalid_manifest", error)


def _wiki_error_result(command: str, code: str, error: Exception) -> CommandResult:
    return CommandResult(command, False, {}, errors=(Diagnostic(code, str(error)),))
