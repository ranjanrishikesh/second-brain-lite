# Agent command and extraction reference

For Codex and Claude Code agents. Follow the canonical skills and policies before executing these commands. End users work through conversation; see the root README.

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
