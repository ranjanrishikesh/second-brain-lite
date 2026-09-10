# Second Brain Lite

Second Brain Lite is a Git-native, source-grounded personal wiki operated through Codex or Claude Code. It uses ordinary files and `rg`; it has no database, vector service, or daemon.

## Start a brain

1. Create a private repository from this template.
2. Put originals beneath `sources/raw/` without changing the reserved `_versions/` or `_web/` directories.
3. Tell Codex or Claude Code: “initialize this brain.”
4. Review any dependency installation or initial URL approval request.
5. Inspect `sources/ledger.md`; full initialization has no unexplained pending or agent work.

Every manifest-bearing `init`, `sync`, or `source snapshot-url` result must be handled in order. Retain its result ID, revision, and counts; run `./brain --json source consume-sync-result --result-id "$result_id"`; and require its `result_id`, immutable `manifest_path`, corpus revision, exact event counts, effect digest, and `handoff_delivery` to match. The command verifies and drains the complete immutable result and records a durable receipt without acknowledging or changing sources/wiki. Read `manifest_path` only for exact rewrites, new-active IDs, and gaps. When non-null, read only the receipt-bound immutable `handoff_delivery`, whose typed items map to consumed handoff effects; never route from response summaries. Verify, drain, durably deduplicate, and then separately run `./brain --json source acknowledge-sync-result --result-id "$result_id"`. Acknowledgement requires the matching receipt and is idempotent. Finish this before dependent mutation or handoff work. For a rendered `handoff.kind == rendered_web_capture`, complete the rendered `snapshot-url` first. If normal allowlisted processing directly activates a representation, require non-null `SnapshotResult.active_representation` and the matching corpus revision. Only if that snapshot returns a subsequent `handoff.kind == extraction` does the agent stage and register it; then `data.registration.active_representation` and the corpus revision prove activation, while the immutable snapshot may remain null. A complete authenticated wiki search with no supported record proceeds to the three source passes; it never constructs an empty evidence packet.

## Ask a question

Ask normally. The agent synchronizes new sources, searches the wiki, runs three widening `rg` passes when needed, persists one evolving topic record and qualifying pages, and validates citations and links.

## Add new sources

Copy them into `sources/raw/`. The next substantive question runs incremental synchronization: it inventories against the maintained ledger, processes eligible new or changed inputs, and does not reconvert unchanged sources. Use `./brain --json status` at any time.

## Approved extraction allowlist

`config/extractors.toml` is authoritative. Never edit the allowlist without explicit approval. `./brain --json doctor` reports which selected implementation is installed and prints the repository-defined installation recipes; it never installs anything.

| Registry ID | Extensions | Preferred → fallback behavior |
|---|---|---|
| `text` | `.txt`, `.md`, `.markdown` | Built-in UTF-8 Markdown/text normalization |
| `tabular` | `.csv`, `.tsv` | Built-in deterministic Markdown tables |
| `json` | `.json`, `.jsonl`, `.ndjson` | Built-in sorted JSON Markdown representation |
| `html` | `.html`, `.htm`, `.xhtml` | Pandoc → standard-library HTML parser |
| `pdf` | `.pdf` | `pdftotext -layout` → PyMuPDF |
| `docx` | `.docx` | Pandoc → python-docx |
| `pptx` | `.pptx` | python-pptx → isolated headless LibreOffice |
| `xlsx` | `.xlsx` | openpyxl → isolated headless LibreOffice |
| `image` | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.webp` | Tesseract for text-focused images → approved agent handoff for complex images |
| `webpage` | `.url.md` | Explicitly approved immutable web capture → normal extraction |

## Direct CLI use

Run commands from the repository root:

```bash
./brain --json doctor
./brain --json init
./brain --json sync
./brain --json source consume-sync-result --result-id "$result_id"
./brain --json source acknowledge-sync-result --result-id "$result_id"
./brain --json status
./brain --json search --scope wiki --term "Alpha"
./brain --json search --scope sources --pass discovery --term "Alpha" --context 3
./brain --json search --scope sources --pass expansion --term "Beta relationship" --context 3
./brain --json search --scope sources --pass verification --term "Alpha exception" --context 3
./brain --json search --cursor OPAQUE_TOKEN
./brain --json source snapshot-url --source-id src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source snapshot-url --url https://example.com/report --description "Example report" --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source snapshot-url --source-id src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --rendered-staging-path .brain/web-staging/hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/page.html --handoff-id hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --retrieved-at 2026-09-04T12:00:00Z --final-url https://example.com/report --detected-media-type text/html --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source adopt-version src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --candidate-sha256 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --approval-note "user approved this source replacement"
./brain --json source register-extraction --handoff-id hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --staging-path .brain/agent-staging/hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/output.md --anchors-json '[{"kind":"page","value":"1"}]' --quality-state ok --note "faithful agent extraction"
./brain --json links candidates wiki/pages/alpha.md --term "Alpha"
./brain --json links candidates --cursor OPAQUE_TOKEN
./brain --json links check
./brain --json wiki apply --manifest .brain/wiki-staging/example/manifest.json
./brain --json wiki recover
./brain --json validate --full
```

Direct `./brain --json init` cannot drain `needs_agent` work; use “initialize this brain” for the full agent-assisted workflow. A rendered `handoff.kind == rendered_web_capture` uses the rendered `snapshot-url` form first: if normal allowlisted processing directly activates a representation, verify non-null `SnapshotResult.active_representation` and the matching corpus revision. Only a subsequent `handoff.kind == extraction` uses `register-extraction`; then `data.registration.active_representation` and the corpus revision prove activation, while the immutable snapshot may remain null. The three source research commands start exactly one discovery, one expansion, and one verification logical run; repeat `--term` within the initial invocation and drain its opaque cursor before starting the next pass.

## Privacy, Git, symlinks, and size

Raw, extracted, ledger, and wiki files are ordinary Git objects; this template does not use Git LFS. Commit `sources/raw/`, `sources/extracted/`, `sources/ledger/`, `sources/ledger.md`, and `wiki/` with the brain so immutable evidence, searchable representations, machine/human ledger state, and synthesized knowledge travel together. Recommend a private remote, warn about provider file-size limits, and explain that deleting current files does not erase private data from Git history. Windows checkout needs Developer Mode with Git symlink support or WSL; `./brain --json validate` rejects a text-file copy of `CLAUDE.md` or a copied `.claude/skills` directory.

## Approval boundaries

Ask before public web access, dependency installation, extractor allowlist changes, source mutation/adoption, destructive wiki edits, sourced-claim removal, and materially contradictory interpretations. Every used public source is saved, extracted, ledgered, and cited locally; public web browsing never occurs silently. Unused candidates are not persisted.

## Development and handoff

The repository does not infer sessions or auto-commit. Keep one logical commit for a completed conversation or PR through the chosen host/squash workflow. Readiness requires `./brain --json validate --full` and a current, gap-free init, sync, or status report: no unresolved pending, failed, unsupported, warning, integrity, approval, agent, or coverage gaps. Run `python3 -m pytest -v` before handoff.
