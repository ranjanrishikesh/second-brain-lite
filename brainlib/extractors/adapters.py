from __future__ import annotations

import hashlib
import csv
import io
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import ExitStack, contextmanager
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Iterator

from ..contracts import (
    Anchor,
    Derivation,
    ProcessingAttempt,
    SourceState,
    derivation_id,
)
from ..diagnostics import Diagnostic
from ..inventory import InventoryAccessError
from ..layout import RepoPaths
from ..ledger import derive_extraction_path
from ..registry import (
    ResolvedConverter,
    _bounded_wait_or_reap_later,
    _wait_for_posix_leader_without_reaping,
    build_converter_argv,
    effective_extractor_version,
)
from ..sync import ProcessResult
from .native import (
    ANCHOR_KINDS,
    MARKER,
    ExtractedPayload,
    ExtractionQualityError,
    anchor_html_id,
    decode_utf8,
    html_text,
    json_payload,
    numbered_payload,
    require_text,
    section_payload,
    sheets_payload,
    tabular_payload,
    text_payload,
)
from .processor import CommandExecution, Job, RunCommand
from .python_driver import PYTHON_ADAPTERS


ExtractionResult = ProcessResult
_STDERR_LIMIT = 16 * 1024
_COMMAND_RECIPES = {
    ("pdf", "poppler.pdftotext"): (
        "pdftotext",
        ("-layout", "-enc", "UTF-8", "{input}", "{output}"),
    ),
    ("html", "pandoc"): (
        "pandoc",
        ("--from=html", "--to=gfm", "--wrap=none", "{input}", "--output", "{output}"),
    ),
    ("docx", "pandoc"): (
        "pandoc",
        ("--from=docx", "--to=gfm", "--wrap=none", "{input}", "--output", "{output}"),
    ),
    ("pptx", "libreoffice"): (
        "soffice",
        ("--headless", "--convert-to", "txt", "--outdir", "{output}", "{input}"),
    ),
    ("xlsx", "libreoffice"): (
        "soffice",
        ("--headless", "--convert-to", "csv", "--outdir", "{output}", "{input}"),
    ),
    ("image", "tesseract"): ("tesseract", ("{input}", "stdout", "-l", "eng")),
}


@dataclass(frozen=True)
class PublishedArtifact:
    output_path: PurePosixPath
    sha256: str
    byte_size: int
    mtime_ns: int


class _AnchorHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.identifiers: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {
            "script",
            "style",
            "template",
            "head",
            "noscript",
            "iframe",
            "object",
            "pre",
            "code",
        } or any(
            name in {"hidden", "style"} or (name == "aria-hidden" and value == "true")
            for name, value in attrs
        ):
            self.hidden.append(tag)
        for name, value in attrs:
            if name in {"id", "name"} and value is not None:
                if value.partition(":")[0] in ANCHOR_KINDS:
                    if (
                        self.hidden
                        or tag != "a"
                        or attrs != [("id", value)]
                        or self.get_starttag_text() != f'<a id="{value}">'
                    ):
                        raise ExtractionQualityError("noncanonical anchor HTML")
                    self.identifiers.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.hidden:
            index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
            del self.hidden[index:]


def _markdown_escaped(text: str, position: int) -> bool:
    start = position
    while start and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 == 1


def _visible_markdown_html(text: str) -> str:
    """Mask code before HTML parsing, preserving block/paragraph boundaries."""

    fence = ""
    visible: list[str] = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        in_fence = bool(fence)
        if match:
            token = match[1]
            if not fence:
                fence = token
                in_fence = True
            elif (
                token[0] == fence[0]
                and len(token) >= len(fence)
                and not line[match.end() :].strip()
            ):
                fence = ""
        if MARKER.search(line) and line.startswith(("    ", "\t", ">")):
            raise ExtractionQualityError("anchor is not navigable Markdown")
        visible.append(re.sub(r"[^\n]", " ", line) if in_fence else line)
    text = "".join(visible)

    # A code span ends at the next run of exactly the opening length, even
    # across line breaks. Blank lines end paragraphs and cannot join spans.
    # Index closing runs backwards to avoid quadratic unmatched-run scans.
    tokens = list(re.finditer(r"`+|\n[ \t]*\n", text))
    following: dict[int, int] = {}
    closing: list[int | None] = [None] * len(tokens)
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if token[0].startswith("\n"):
            following.clear()
            continue
        length = len(token[0])
        opening_length = length - int(_markdown_escaped(text, token.start()))
        closing[index] = following.get(opening_length)
        following[length] = index
    parts: list[str] = []
    cursor = index = 0
    while index < len(tokens):
        end_index = closing[index]
        if end_index is None:
            index += 1
            continue
        start = tokens[index].start()
        start += int(_markdown_escaped(text, start))
        end = tokens[end_index].end()
        parts.append(text[cursor:start])
        parts.append(re.sub(r"[^\n]", " ", text[start:end]))
        cursor = end
        index = end_index + 1
    parts.append(text[cursor:])
    # Escaped non-anchor HTML must not affect the HTML parser's hidden stack.
    return re.sub(
        r"(\\+)<",
        lambda match: match[1] + ("&lt;" if len(match[1]) % 2 else "<"),
        "".join(parts),
    )


def validate_markdown(
    markdown: str | bytes,
    anchors: tuple[Anchor, ...],
    *,
    expected_anchors: tuple[str, ...],
    max_output_bytes: int,
) -> bytes:
    """Validate the exact bounded bytes shared by deterministic and agent writers."""

    try:
        body = markdown.encode("utf-8") if isinstance(markdown, str) else markdown
    except UnicodeEncodeError as error:
        raise ExtractionQualityError(
            "extraction is not UTF-8", code="invalid_utf8"
        ) from error
    text = decode_utf8(body, max_output_bytes=max_output_bytes)
    require_text(text)
    for match in MARKER.finditer(text):
        if _markdown_escaped(text, match.start()):
            raise ExtractionQualityError("anchor HTML is Markdown-escaped")
    visible = _visible_markdown_html(text)
    markers = [anchor_html_id(anchor) for anchor in anchors]
    recorded_markers = Counter(match[0] for match in MARKER.finditer(text))
    if (
        not markers
        or len(set(markers)) != len(markers)
        or set(anchor.kind for anchor in anchors) != set(expected_anchors)
        or any(count != 1 for count in recorded_markers.values())
        or set(recorded_markers) != set(markers)
    ):
        raise ExtractionQualityError("missing, duplicate, or unexpected anchors")
    if Counter(match[0] for match in MARKER.finditer(visible)) != recorded_markers:
        raise ExtractionQualityError("anchor is hidden inside Markdown code")
    parser = _AnchorHTML()
    parser.feed(visible)
    parser.close()
    expected_ids = [f"{anchor.kind}:{anchor.value}" for anchor in anchors]
    if sorted(parser.identifiers) != sorted(expected_ids):
        raise ExtractionQualityError("anchor HTML does not match recorded anchors")
    require_text(MARKER.sub("", text))
    return body


def _same_file(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def _publication_parent(
    paths: RepoPaths, destination: Path
) -> Iterator[tuple[int, object]]:
    try:
        relative = destination.relative_to(paths.root)
    except ValueError as error:
        raise ExtractionQualityError(
            "output is outside the repository", code="unsafe_output_path"
        ) from error
    if (
        relative.parts[:2] != ("sources", "extracted")
        or len(relative.parts) < 3
        or any(part in {".", ".."} for part in relative.parts)
        or destination.suffix != ".md"
        or paths.extracted != paths.root / "sources/extracted"
    ):
        raise ExtractionQualityError(
            "output must be Markdown below sources/extracted", code="unsafe_output_path"
        )
    descriptors: list[int] = []
    entries: list[tuple[int, str, int]] = []
    try:
        root = os.open(paths.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root)
        for part in relative.parts[:-1]:
            parent = descriptors[-1]
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            descriptors.append(descriptor)
            entries.append((parent, part, descriptor))

        def revalidate() -> None:
            root_stat = paths.root.stat(follow_symlinks=False)
            pinned_root = os.fstat(root)
            if (root_stat.st_dev, root_stat.st_ino) != (
                pinned_root.st_dev,
                pinned_root.st_ino,
            ):
                raise ExtractionQualityError(
                    "repository root changed", code="unsafe_output_path"
                )
            for parent, name, descriptor in entries:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                pinned = os.fstat(descriptor)
                if not stat.S_ISDIR(observed.st_mode) or (
                    observed.st_dev,
                    observed.st_ino,
                ) != (pinned.st_dev, pinned.st_ino):
                    raise ExtractionQualityError(
                        "output directory changed", code="unsafe_output_path"
                    )

        revalidate()
        yield descriptors[-1], revalidate
        revalidate()
    except OSError as error:
        raise ExtractionQualityError(
            "unsafe or inaccessible output path", code="unsafe_output_path"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _existing_artifact(
    parent: int, name: str, checksum: str, limit: int
) -> os.stat_result:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ValueError("not a bounded regular file")
            digest = hashlib.sha256()
            offset = 0
            while offset <= before.st_size:
                chunk = os.pread(
                    descriptor, min(1024 * 1024, before.st_size + 1 - offset), offset
                )
                if not chunk:
                    break
                digest.update(chunk)
                offset += len(chunk)
            after = os.fstat(descriptor)
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                offset != before.st_size
                or digest.hexdigest() != checksum
                or _same_file(before) != _same_file(after)
                or _same_file(after) != _same_file(entry)
            ):
                raise ValueError("existing output differs or changed")
            return after
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as error:
        raise ExtractionQualityError(
            "canonical output already exists with different or unsafe bytes",
            code="output_path_collision",
        ) from error


def publish_markdown_artifact(
    markdown: str | bytes,
    anchors: tuple[Anchor, ...],
    *,
    paths: RepoPaths,
    destination: Path,
    expected_anchors: tuple[str, ...],
    max_output_bytes: int,
) -> PublishedArtifact:
    """Publish validated bytes without overwriting any existing canonical artifact.

    Provenance deliberately belongs to the caller. Atomic exclusive linking, not
    check-then-replace, protects simultaneous attempts without a ledger/lock.
    """

    body = validate_markdown(
        markdown,
        anchors,
        expected_anchors=expected_anchors,
        max_output_bytes=max_output_bytes,
    )
    checksum = hashlib.sha256(body).hexdigest()
    with _publication_parent(paths, destination) as (parent, revalidate):
        temporary = ".brain-tmp-artifact-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            revalidate()
            try:
                os.link(
                    temporary,
                    destination.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _existing_artifact(parent, destination.name, checksum, max_output_bytes)
        finally:
            os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
        observed = _existing_artifact(
            parent, destination.name, checksum, max_output_bytes
        )
        revalidate()
    return PublishedArtifact(
        paths.repo_relative(destination),
        checksum,
        observed.st_size,
        observed.st_mtime_ns,
    )


def run_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> CommandExecution:
    """POSIX-only inherited-descriptor runner with bounded in-memory diagnostics."""

    if os.name != "posix" or not pass_fds:
        raise OSError("converter requires the POSIX inherited-descriptor backend")
    with (
        tempfile.TemporaryFile(dir=cwd) as stdout,
        tempfile.TemporaryFile(dir=cwd) as stderr,
    ):
        process = subprocess.Popen(
            list(argv),
            shell=False,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            # Keep the dead leader pinned until its process group is killed;
            # otherwise PID reuse could target another process group.
            returncode = _wait_for_posix_leader_without_reaping(
                process,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                _bounded_wait_or_reap_later(process, deadline=deadline)
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read(max_output_bytes + 1)
        err = stderr.read(_STDERR_LIMIT + 1)
        return CommandExecution(
            returncode,
            out[:max_output_bytes],
            err[:_STDERR_LIMIT],
            len(out) > max_output_bytes,
            len(err) > _STDERR_LIMIT,
        )


def _failure(
    job: Job, code: str, message: str, *, state: SourceState = SourceState.FAILED
) -> ProcessResult:
    diagnostic = Diagnostic(code, message, job.item.fingerprint.path)
    return ProcessResult(
        state, None, _attempt(job, state, (diagnostic,)), (diagnostic,)
    )


def _attempt(
    job: Job, state: SourceState, diagnostics: tuple[Diagnostic, ...]
) -> ProcessingAttempt:
    return ProcessingAttempt(
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.context.config_sha256,
        job.context.prerequisite_digest,
        state,
        job.context.attempted_at,
        tuple(item.code for item in diagnostics),
    )


def _resolution_changed(job: Job, resolved: ResolvedConverter) -> bool:
    return (
        resolved.prerequisite_digest != job.context.prerequisite_digest
        or job.context.config_sha256 != job.extractor.config_sha256
        or job.context.extractor_version
        != effective_extractor_version(job.extractor, resolved.prerequisite_digest)
        or resolved.converter not in (job.extractor.preferred, *job.extractor.fallbacks)
        or type(resolved.detected_version) is not str
        or not resolved.detected_version.strip()
    )


def _staged_text(job: Job) -> str:
    stage = job.staged_input
    if stage.byte_size > job.extractor.max_output_bytes:
        raise ExtractionQualityError(
            "native input exceeds extraction limit", code="output_too_large"
        )
    chunks: list[bytes] = []
    offset = 0
    while offset <= stage.byte_size:
        chunk = os.pread(
            stage.descriptor, min(1024 * 1024, stage.byte_size + 1 - offset), offset
        )
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    if offset != stage.byte_size:
        raise InventoryAccessError("staged input size changed during native extraction")
    body = b"".join(chunks)
    return decode_utf8(body, max_output_bytes=job.extractor.max_output_bytes)


def _validate_job_stage(job: Job) -> None:
    stage = job.staged_input
    stage.revalidate()
    if (
        job.context.input_sha256 != stage.sha256
        or job.context.input_descriptor != stage.descriptor
        or job.context.input_path != stage.descriptor_path
        or job.item.fingerprint.path != stage.logical_path
        or job.item.fingerprint.byte_size != stage.byte_size
    ):
        raise InventoryAccessError("job does not match its pinned stage")


def _read_output(
    path: Path, limit: int, *, directory_descriptor: int | None = None
) -> str:
    try:
        descriptor = os.open(
            path if directory_descriptor is None else path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_descriptor,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ExtractionQualityError(
                    "converter output is not a regular file",
                    code="unsafe_converter_output",
                )
            if before.st_size > limit:
                raise ExtractionQualityError(
                    "converter output exceeds byte limit", code="output_too_large"
                )
            body = os.pread(descriptor, limit + 1, 0)
            after = os.fstat(descriptor)
            if (
                len(body) != before.st_size
                or _same_file(before) != _same_file(after)
                or _same_file(after)
                != _same_file(
                    os.stat(
                        path if directory_descriptor is None else path.name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                )
            ):
                raise ExtractionQualityError(
                    "converter output changed while reading",
                    code="unsafe_converter_output",
                )
        finally:
            os.close(descriptor)
    except FileNotFoundError as error:
        raise ExtractionQualityError(
            "converter did not write its output file", code="missing_converter_output"
        ) from error
    except OSError as error:
        raise ExtractionQualityError(
            "converter output cannot be safely read", code="unsafe_converter_output"
        ) from error
    text = decode_utf8(body, max_output_bytes=limit)
    require_text(text)
    return text


def _normalize_external(text: str, job: Job) -> ExtractedPayload:
    kind = job.extractor.extractor_id
    if kind == "html":
        return section_payload(html_text(text))
    # Converter text is evidence, never an authority for location metadata.
    # The payload builders quote literal markers before generating boundaries.
    if kind == "pdf":
        pages = text.replace("\r\n", "\n").split("\f")
        if len(pages) > 1 and not pages[-1].strip():
            pages.pop()
        return numbered_payload(pages, "page")
    if kind == "docx":
        return section_payload(text)
    if kind == "image":
        return numbered_payload([text], "block")
    raise ExtractionQualityError(
        "converter normalization is unavailable", code="unsupported_converter"
    )


def _office_structure(job: Job) -> list[str]:
    """Read bounded package metadata from the inherited input, never raw paths."""

    try:
        with (
            job.staged_input.descriptor_path.open("rb") as stream,
            zipfile.ZipFile(stream) as package,
        ):
            if job.extractor.extractor_id == "pptx":
                slides = [
                    name
                    for name in package.namelist()
                    if re.fullmatch(r"ppt/slides/slide[1-9][0-9]*\.xml", name)
                ]
                if not slides or len(set(slides)) != len(slides):
                    raise ValueError("missing or duplicate slides")
                return sorted(slides)
            info = package.getinfo("xl/workbook.xml")
            if info.file_size > job.extractor.max_output_bytes:
                raise ExtractionQualityError(
                    "workbook metadata exceeds limit", code="output_too_large"
                )
            with package.open(info) as xml_stream:
                body = xml_stream.read(job.extractor.max_output_bytes + 1)
            if len(body) > job.extractor.max_output_bytes or re.search(
                rb"<!\s*(?:DOCTYPE|ENTITY)", body, re.I
            ):
                raise ValueError("unsafe workbook metadata")
            root = ET.fromstring(body)
            names = [
                entry.attrib["name"] for entry in root.findall("{*}sheets/{*}sheet")
            ]
            if not names or len(set(names)) != len(names):
                raise ValueError("missing or duplicate sheet names")
            return names
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        if isinstance(error, ExtractionQualityError):
            raise
        raise ExtractionQualityError(
            "malformed Office package", code="malformed_input"
        ) from error


def _libreoffice_payload(
    job: Job, output: Path, *, directory_descriptor: int
) -> ExtractedPayload:
    names = _office_structure(job)
    suffix = ".csv" if job.extractor.extractor_id == "xlsx" else ".txt"
    files = sorted(output / name for name in os.listdir(directory_descriptor))
    if any(
        not stat.S_ISREG(
            os.stat(
                path.name, dir_fd=directory_descriptor, follow_symlinks=False
            ).st_mode
        )
        or path.suffix != suffix
        for path in files
    ):
        raise ExtractionQualityError(
            "unsafe LibreOffice output", code="unsafe_converter_output"
        )
    if job.extractor.extractor_id == "pptx":
        if len(files) != 1:
            raise ExtractionQualityError(
                "LibreOffice did not produce one text artifact",
                code="missing_converter_output",
            )
        text = _read_output(
            files[0],
            job.extractor.max_output_bytes,
            directory_descriptor=directory_descriptor,
        )
        slides = text.split("\f")
        if len(slides) > 1 and not slides[-1].strip():
            slides.pop()
        if len(slides) != len(names):
            raise ExtractionQualityError(
                "LibreOffice output cannot establish every slide",
                code="incomplete_slide_coverage",
            )
        return numbered_payload(slides, "slide")
    if len(files) != len(names):
        raise ExtractionQualityError(
            "LibreOffice output cannot establish every sheet",
            code="incomplete_sheet_coverage",
        )
    if len(names) == 1:
        ordered = files
    else:
        by_name = {path.stem: path for path in files}
        if set(by_name) != set(names):
            raise ExtractionQualityError(
                "LibreOffice sheet names cannot be mapped to the workbook",
                code="incomplete_sheet_coverage",
            )
        ordered = [by_name[name] for name in names]
    remaining = job.extractor.max_output_bytes
    sheets = []
    for name, path in zip(names, ordered):
        text = _read_output(path, remaining, directory_descriptor=directory_descriptor)
        remaining -= len(text.encode("utf-8"))
        try:
            rows = list(csv.reader(io.StringIO(text), strict=True))
        except csv.Error as error:
            raise ExtractionQualityError(
                "malformed exported sheet", code="malformed_input"
            ) from error
        sheets.append((name, rows))
    return sheets_payload(sheets)


def extract_payload(
    job: Job,
    resolved: ResolvedConverter,
    *,
    paths: RepoPaths,
    run: RunCommand,
    staging_dir: Path | None = None,
) -> ExtractedPayload:
    if staging_dir is None:
        with tempfile.TemporaryDirectory(
            prefix=".brain-tmp-", dir=paths.root
        ) as directory:
            return extract_payload(
                job, resolved, paths=paths, run=run, staging_dir=Path(directory)
            )
    with ExitStack() as owned:
        descriptor = os.open(staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        owned.callback(os.close, descriptor)
        return _extract_in_directory(
            job,
            resolved,
            paths=paths,
            run=run,
            staging_dir=staging_dir,
            directory_descriptor=descriptor,
            owned=owned,
        )


def _extract_in_directory(
    job: Job,
    resolved: ResolvedConverter,
    *,
    paths: RepoPaths,
    run: RunCommand,
    staging_dir: Path,
    directory_descriptor: int,
    owned: ExitStack,
) -> ExtractedPayload:
    _validate_job_stage(job)
    before_directory = os.fstat(directory_descriptor)
    if (
        not stat.S_ISDIR(before_directory.st_mode)
        or before_directory.st_mode & 0o777 != 0o700
    ):
        raise ExtractionQualityError(
            "unsafe converter output directory", code="unsafe_converter_output"
        )

    def check_directory() -> None:
        after = staging_dir.stat(follow_symlinks=False)
        if not stat.S_ISDIR(after.st_mode) or (
            before_directory.st_dev,
            before_directory.st_ino,
            before_directory.st_mode,
        ) != (after.st_dev, after.st_ino, after.st_mode):
            raise ExtractionQualityError(
                "converter output directory changed", code="unsafe_converter_output"
            )

    check_directory()
    converter = resolved.converter
    identifier = converter.converter_id
    if identifier.startswith("builtin."):
        output = staging_dir / "converted.md"
        argv = build_converter_argv(
            converter, input_path=job.staged_input.descriptor_path, output_path=output
        )
        if converter.argv_template != ("{input}", "{output}") or argv != (
            str(job.staged_input.descriptor_path),
            str(output),
        ):
            raise ExtractionQualityError(
                "unregistered builtin adapter recipe", code="unsupported_converter"
            )
        text = _staged_text(job)
        if identifier == "builtin.text":
            payload = text_payload(text)
        elif identifier == "builtin.tabular":
            payload = tabular_payload(
                text, delimiter="\t" if job.item.extension == ".tsv" else ","
            )
        elif identifier == "builtin.json":
            payload = json_payload(
                text, lines=job.item.extension in {".jsonl", ".ndjson"}
            )
        elif identifier == "builtin.html":
            payload = section_payload(html_text(text))
        else:
            raise ExtractionQualityError(
                "converter is not an extraction adapter", code="unsupported_converter"
            )
        body = validate_markdown(
            payload.markdown,
            payload.anchors,
            expected_anchors=job.extractor.expected_anchors,
            max_output_bytes=job.extractor.max_output_bytes,
        )
        descriptor = os.open(
            output.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        payload = ExtractedPayload(
            _read_output(
                output,
                job.extractor.max_output_bytes,
                directory_descriptor=directory_descriptor,
            ),
            payload.anchors,
            payload.warning,
        )
    else:
        if identifier not in PYTHON_ADAPTERS:
            recipe = _COMMAND_RECIPES.get((job.extractor.extractor_id, identifier))
            if (
                recipe is None
                or (converter.executable, converter.argv_template) != recipe
                or converter.python_distribution is not None
            ):
                raise ExtractionQualityError(
                    "unregistered command adapter recipe", code="unsupported_converter"
                )
        output = staging_dir / (
            "libreoffice-output" if identifier == "libreoffice" else "converted.md"
        )
        if identifier == "libreoffice":
            os.mkdir(output.name, mode=0o700, dir_fd=directory_descriptor)
            office_descriptor = os.open(
                output.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            owned.callback(os.close, office_descriptor)
            office_directory = os.fstat(office_descriptor)
        argv = build_converter_argv(
            converter, input_path=job.staged_input.descriptor_path, output_path=output
        )
        if identifier in PYTHON_ADAPTERS:
            if (
                converter.python_distribution
                != PYTHON_ADAPTERS[identifier].distribution
                or converter.executable is not None
                or converter.argv_template != ("{input}", "{output}")
                or argv != (str(job.staged_input.descriptor_path), str(output))
            ):
                raise ExtractionQualityError(
                    "unregistered Python adapter recipe", code="unsupported_converter"
                )
            interpreter = Path(sys.executable)
            if (
                not interpreter.is_absolute()
                or not interpreter.is_file()
                or not os.access(interpreter, os.X_OK)
            ):
                raise OSError("current Python interpreter is unavailable")
            argv = (
                sys.executable,
                "-I",
                str(Path(__file__).with_name("python_driver.py").resolve(strict=True)),
                identifier,
                *argv,
                str(job.extractor.max_output_bytes),
            )
        if identifier == "libreoffice":
            argv = (
                *argv,
                "-env:UserInstallation="
                + (staging_dir / "libreoffice-profile").as_uri(),
            )
        execution = run(
            argv,
            cwd=staging_dir,
            timeout_seconds=job.extractor.timeout_seconds,
            max_output_bytes=job.extractor.max_output_bytes,
            pass_fds=(job.staged_input.descriptor,),
        )
        check_directory()
        if (
            execution.stdout_truncated
            or execution.stderr_truncated
            or len(execution.stdout) > job.extractor.max_output_bytes
            or len(execution.stderr) > _STDERR_LIMIT
        ):
            raise ExtractionQualityError(
                "converter diagnostic output was truncated",
                code="converter_output_truncated",
            )
        if execution.returncode != 0:
            raise ExtractionQualityError(
                "converter returned a nonzero exit status", code="converter_failed"
            )
        if identifier == "tesseract":
            text = decode_utf8(
                execution.stdout, max_output_bytes=job.extractor.max_output_bytes
            )
            if (
                sum(character.isalnum() for character in text) < 10
                or len(text.split()) < 3
            ):
                raise ExtractionQualityError(
                    "image needs visual interpretation or clearer OCR",
                    code="low_confidence_ocr",
                )
            descriptor = os.open(
                output.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(execution.stdout)
        if identifier == "libreoffice":
            observed = output.stat(follow_symlinks=False)
            if not stat.S_ISDIR(observed.st_mode) or (
                observed.st_dev,
                observed.st_ino,
            ) != (office_directory.st_dev, office_directory.st_ino):
                raise ExtractionQualityError(
                    "LibreOffice output directory changed",
                    code="unsafe_converter_output",
                )
            payload = _libreoffice_payload(
                job, output, directory_descriptor=office_descriptor
            )
        else:
            text = _read_output(
                output,
                job.extractor.max_output_bytes,
                directory_descriptor=directory_descriptor,
            )
            if identifier in PYTHON_ADAPTERS:
                # Only our fixed driver generates metadata. Its payload builders
                # escape source markers before adding locations from parsed parts.
                payload = ExtractedPayload(
                    text,
                    tuple(
                        Anchor(match[1], match[2]) for match in MARKER.finditer(text)
                    ),
                    None,
                )
            else:
                payload = _normalize_external(text, job)
    job.staged_input.revalidate()
    check_directory()
    return payload


def publish_payload(
    job: Job,
    payload: ExtractedPayload,
    *,
    paths: RepoPaths,
    resolved: ResolvedConverter,
) -> ProcessResult:
    if _resolution_changed(job, resolved):
        return _failure(
            job,
            "prerequisite_changed",
            "The converter or recipe changed; retry with current prerequisites.",
            state=SourceState.PENDING,
        )
    identifier = derivation_id(
        source_sha256=job.context.input_sha256,
        extractor_id=job.extractor.extractor_id,
        extractor_version=job.context.extractor_version,
        config_sha256=job.extractor.config_sha256,
    )
    destination = paths.extracted / derive_extraction_path(
        job.item.fingerprint.path, job.context.input_sha256, identifier
    )
    _validate_job_stage(job)
    try:
        artifact = publish_markdown_artifact(
            payload.markdown,
            payload.anchors,
            paths=paths,
            destination=destination,
            expected_anchors=job.extractor.expected_anchors,
            max_output_bytes=job.extractor.max_output_bytes,
        )
    except ExtractionQualityError as error:
        return _failure(job, error.code, str(error))
    job.staged_input.revalidate()
    diagnostics = () if payload.warning is None else (payload.warning,)
    state = SourceState.OK if payload.warning is None else SourceState.WARNING
    derivation = Derivation(
        identifier,
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        artifact.output_path,
        artifact.sha256,
        artifact.byte_size,
        artifact.mtime_ns,
        "ok" if payload.warning is None else "warning",
        payload.anchors,
        job.context.attempted_at,
        method="deterministic",
        method_metadata={
            "converter_id": resolved.converter.converter_id,
            "converter_version": resolved.detected_version,
        },
    )
    return ProcessResult(
        state, derivation, _attempt(job, state, diagnostics), diagnostics
    )


def run_job(
    job: Job,
    *,
    paths: RepoPaths,
    run: RunCommand,
    resolved: ResolvedConverter,
    staging_dir: Path | None = None,
) -> ProcessResult:
    if _resolution_changed(job, resolved):
        return _failure(
            job,
            "prerequisite_changed",
            "The converter or recipe changed; retry with current prerequisites.",
            state=SourceState.PENDING,
        )
    if (
        job.extractor.extractor_id == "image"
        and job.extractor.agent_fallback
        and any(
            diagnostic.code == "complex_image" for diagnostic in job.record.diagnostics
        )
    ):
        return _failure(
            job,
            "complex_image",
            "Image requires visual interpretation.",
            state=SourceState.NEEDS_AGENT,
        )
    if staging_dir is None:
        with tempfile.TemporaryDirectory(
            prefix=".brain-tmp-", dir=paths.root
        ) as directory:
            return run_job(
                job,
                paths=paths,
                run=run,
                resolved=resolved,
                staging_dir=Path(directory),
            )
    try:
        payload = extract_payload(
            job, resolved, paths=paths, run=run, staging_dir=staging_dir
        )
        return publish_payload(job, payload, paths=paths, resolved=resolved)
    except ExtractionQualityError as error:
        state = (
            SourceState.NEEDS_AGENT
            if job.extractor.extractor_id == "image"
            and job.extractor.agent_fallback
            and error.code
            in {
                "low_confidence_ocr",
                "empty_extraction",
                "converter_failed",
                "missing_converter_output",
            }
            else SourceState.FAILED
        )
        return _failure(job, error.code, str(error), state=state)
    except subprocess.TimeoutExpired:
        return _failure(
            job,
            "converter_timeout",
            "Converter exceeded its timeout.",
            state=SourceState.NEEDS_AGENT
            if job.extractor.extractor_id == "image" and job.extractor.agent_fallback
            else SourceState.FAILED,
        )
    except InventoryAccessError:
        raise
    except OSError:
        return _failure(
            job,
            "converter_unavailable",
            "Converter or extraction output is unavailable.",
            state=SourceState.NEEDS_AGENT
            if job.extractor.extractor_id == "image" and job.extractor.agent_fallback
            else SourceState.FAILED,
        )
