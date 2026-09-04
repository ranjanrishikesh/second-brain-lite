# Second Brain Lite: Design Specification

**Date:** 2026-09-04

**Status:** Approved design

**Target:** A repository template used through Codex or Claude Code

## 1. Summary

Second Brain Lite is a Git-native, source-grounded personal knowledge system. A user creates a repository from an empty template, adds private source material, initializes the corpus, and then grows an interconnected Markdown wiki by asking questions.

The system deliberately avoids a database, vector index, daemon, or always-on service. Its search engine is `rg` (ripgrep), with `grep` available for simple pipeline checks and fallback use. A small repository-local CLI owns deterministic mechanics such as inventory, extraction, checksums, ledger state, and validation. LLM-powered skills and agents own semantic work such as search-term expansion, evidence assessment, synthesis, page creation, and link decisions.

The central rule is:

> Code performs mechanics; LLMs perform judgment; Markdown and Git preserve truth.

## 2. Goals

1. Keep original user material as the authoritative source of truth.
2. Make binary and structured sources searchable by producing committed text or Markdown representations.
3. Answer from existing wiki knowledge first, then perform a mandatory three-pass source investigation when the wiki is insufficient.
4. Preserve claim-level provenance from wiki prose to an exact source version and location.
5. Grow small, evidence-backed, densely interconnected Markdown pages through ordinary questions.
6. Work consistently in Codex and Claude Code through shared skills, focused agents, and a small CLI.
7. Remain understandable and repairable with ordinary files, Git, shell tools, and documented commands.
8. Scale to a large source tree without reconverting or rehashing every unchanged file before every question.
9. Make missing coverage, failed extraction, conflicting evidence, and uncertain conclusions explicit.
10. Keep the distributed template free of personal sources and generated personal knowledge.

## 3. Non-goals

The first version will not include:

- A vector database, embeddings pipeline, graph database, or semantic-search service.
- A background watcher, daemon, or hosted backend.
- Automatic generation of an entire wiki during initialization.
- Audio or video transcription in the core extractor set.
- Automatic public-web research without an explicit user approval.
- Automatic system-package installation.
- Automatic Git session detection, commit creation, branch management, PR creation, or merge behavior.
- A requirement to use Conductor, Obsidian, or any other specific host or notes application.
- OCR as the normal path for text-readable PDFs. Images and exceptional documents may use an agent's vision capability.

## 4. Governing principles

### 4.1 Originals are authoritative

User-provided originals live beneath `sources/raw/`. Agents must not rewrite, normalize, rename, or delete them as a routine operation. User changes are detected and versioned in the ledger. Destructive source operations require explicit approval.

The wiki is a derived, rebuildable interpretation. It may summarize and connect sources, but it must not silently become an unsupported source of canonical facts.

### 4.2 Derived artifacts are committed

The following are ordinary Git-tracked repository content:

- `sources/raw/`
- `sources/extracted/`
- `sources/ledger/`
- `sources/ledger.md`
- `wiki/`

The template neither configures nor uses Git LFS. Raw and extracted artifacts are ordinary Git objects. Users remain free to customize their own repositories later. The README will warn that hosting providers can impose file-size limits and that private information remains in Git history even after deletion from the current tree.

### 4.3 Mechanics and judgment are separated

The CLI performs operations with objectively checkable outputs. It never decides whether evidence is persuasive, whether a topic deserves a page, or how facts relate.

Skills and agents make semantic decisions. They never manually invent checksums, mark extraction successful without validation, or bypass source and graph invariants.

### 4.4 Failures are visible

No operation may imply complete corpus coverage when sources are pending, failed, unsupported, corrupt, encrypted, empty, truncated, timed out, or low-confidence. Answers based on incomplete coverage must identify the gap.

### 4.5 The filesystem stays primary

Machine-readable metadata supports the Markdown corpus but never replaces it. A user must be able to inspect source records, extracted text, wiki pages, citations, and validation results without a proprietary viewer.

## 5. Repository structure

```text
.
├── AGENTS.md                    # concise canonical routing instructions
├── CLAUDE.md -> AGENTS.md       # symlink; one instruction source
├── BRAIN.md                     # operating model and documentation index
├── README.md                    # template setup and user guide
├── brain                        # repo-local CLI launcher
├── brainlib/                    # deterministic Python implementation
│   ├── ledger.py
│   ├── registry.py
│   ├── extractors/
│   └── validators/
├── config/
│   └── extractors.toml          # approved converter allowlist
├── docs/brain/
│   ├── policies/                # source, wiki, citation, web, approval rules
│   ├── workflows/               # initialize, answer, ingest, reconcile
│   ├── schemas/                 # source record, page, and question contracts
│   └── agent-briefs/            # shared semantic responsibilities
├── .agents/skills/              # canonical shared skills
├── .claude/skills/              # links to canonical skills
├── .codex/agents/               # thin Codex agent adapters
├── .claude/agents/              # thin Claude agent adapters
├── sources/
│   ├── raw/                     # immutable user originals
│   │   ├── _versions/           # prior exact bytes, materialized only when needed
│   │   └── _web/                # immutable approved web snapshots and downloads
│   ├── extracted/               # versioned searchable representations
│   ├── ledger/                  # one JSON record per logical source
│   └── ledger.md                # generated human-readable status
├── wiki/
│   ├── pages/                   # atomic interconnected knowledge pages
│   ├── questions/               # one evolving record per topic
│   └── index.md
└── tests/
```

The empty template commits reserved sentinel files where Git requires them to retain empty directories. Generic source discovery explicitly ignores `.gitkeep`, `.keep`, `.DS_Store`, the reserved `_versions/` and `_web/` trees, and documented temporary and lock names. Historical versions and approved web captures enter the ledger only through their dedicated commands, preventing generated snapshots from being rediscovered as unrelated logical sources. Test fixtures live under `tests/fixtures/`, never under the user's source tree.

## 6. Instruction architecture

### 6.1 Root entrypoints

`AGENTS.md` is the canonical root instruction file. It stays concise and routes work instead of embedding every operational rule. `CLAUDE.md` is a repository symlink to `AGENTS.md`.

`BRAIN.md` is the readable operating manual and documentation index. It explains the mental model, directory roles, normal lifecycle, and where to find detailed policies and workflows.

### 6.2 Routing rules

`AGENTS.md` distinguishes knowledge work from repository development:

- A first-time corpus setup request invokes the initialization skill.
- A substantive knowledge question invokes the answer skill.
- Approved external research invokes the web-research skill.
- Manual page or graph work invokes the wiki-maintenance skill.
- Handoff or PR readiness invokes the validation skill.
- A request to change the CLI, tests, documentation, or repository architecture uses the normal software-development workflow and is not archived as a knowledge question.

### 6.3 Shared skills and tool-specific agents

Canonical skills live under `.agents/skills/`. Claude's skill discovery path points to those same skill directories rather than duplicating their contents.

Canonical agent briefs live under `docs/brain/agent-briefs/`. Codex TOML files and Claude agent definitions are thin adapters that tell the agent to load and follow the relevant shared brief. Tool-specific model names are not required by the template; adapters inherit the user's available/default model unless a future, documented need justifies a setting.

## 7. CLI design

The repository exposes an executable `brain` launcher backed by a small Python implementation. The coordinator is standard-library-first and invokes allowlisted external converters through argument arrays rather than interpolated shell commands.

Every command provides concise human output and a structured JSON mode for agents.

### 7.1 Commands

- `brain doctor` inspects the platform, Python environment, `rg`, approved extractors, and missing dependencies. It prints proposed install commands but performs no system install.
- `brain init` inventories and processes the deterministic portion of the full initial corpus. It is idempotent, resumes from ledger checkpoints, and emits a handoff manifest for any `needs_agent` items.
- `brain sync` performs lightweight pre-question reconciliation and processes new, changed, pending, newly supported, or otherwise unsearchable sources according to the retry rules in Section 10.2.
- `brain status` reports corpus revision, counts by ledger state, failures, warnings, and agent-assisted work.
- `brain search` safely invokes `rg` over the wiki or the ledger-selected active source representations.
- `brain source snapshot-url` captures an approved webpage or download, records retrieval metadata, and routes the material through normal extraction.
- `brain source adopt-version` performs the approval-gated adoption of changed bytes for an existing logical source after preserving the prior exact bytes.
- `brain source register-extraction` validates and records an agent-produced representation.
- `brain links candidates` finds possible inbound relationships for a page title, aliases, and important terms.
- `brain links check` validates the Markdown graph.
- `brain validate` runs normal deterministic ledger, extraction, source, citation, and graph checks. `brain validate --full` additionally rehashes every retained content version and derivation and is mandatory before completed handoff or PR readiness.

The CLI has no `ask` command. The conversation is the question interface, and the answer skill orchestrates commands and agents.

### 7.2 Installation policy

When an approved converter is unavailable, `brain doctor` or `brain init` reports:

- The source types affected.
- The preferred approved converter and supported fallbacks.
- The exact platform-appropriate installation commands.
- Whether the dependency is system-level or repository-local.

The calling agent asks the user before installation. Repository-local Python dependencies may be installed in a local environment only after approval. System packages are never installed automatically.

## 8. Extractor registry

`config/extractors.toml` is a committed allowlist. Each entry defines:

- Detected MIME types and recognized extensions.
- A stable extractor identifier.
- Preferred and fallback converters.
- Safe command arguments or an `agent` execution mode.
- Expected output type and naming rules.
- Timeout and output-size limits.
- Quality checks, such as nonempty output and expected page counts.
- Extractor version detection.
- Whether page, slide, sheet, or section anchors are expected.

The template ships with populated preferred and fallback strategies plus install recipes for every core format:

- Markdown, text, CSV, TSV, and JSON use repository-local Python readers and require no converter.
- HTML prefers Pandoc-to-GFM and falls back to a Python HTML text extractor.
- PDF prefers Poppler `pdftotext` with layout and page boundaries and falls back to PyMuPDF.
- DOCX prefers Pandoc-to-GFM and falls back to `python-docx`.
- PPTX prefers `python-pptx` and falls back to a safe headless LibreOffice conversion path.
- XLSX prefers `openpyxl` and falls back to safe per-sheet CSV export through headless LibreOffice.
- Text-focused images prefer Tesseract OCR; complex visual material uses the source-ingestion agent's vision capability.
- Static webpages use a standard HTTP capture adapter; pages that require rendering use an approved agent/browser capture path.

The registry includes install instructions for supported macOS, Linux, and Windows environments. An adapter is considered supported only after its command, anchor behavior, and quality checks pass the fixture suite.

The CLI detects content type rather than trusting only a filename extension. It never executes document macros, embedded executables, or arbitrary commands discovered inside a source.

Adding or changing a registry entry is approval-gated. An agent may research and propose a better converter, but public-web research and software installation retain their own approval gates.

## 9. Source and ledger model

### 9.1 Raw sources

Users place originals beneath `sources/raw/**` and may preserve any directory hierarchy. The generic scanner excludes generated output, ledger data, `.gitkeep`, `.keep`, `.DS_Store`, `_versions/`, `_web/`, documented temporary and lock files, and any symlink whose resolved target escapes `sources/raw/`.

A user-provided URL is represented by `sources/raw/urls/<slug>.url.md` with this required frontmatter:

```yaml
---
kind: url
url: https://example.com/resource
description: A short explanation of what the URL contains.
added: 2026-09-04
---
```

The descriptor is control metadata that establishes a logical web source and helps relevance search; it is not evidence for claims about the remote content. It is therefore exempt from raw-evidence immutability: changing only its description updates metadata, while changing its URL creates a new capture request, deactivates the prior capture for current search, and requires fresh web approval. Prior versions and derivations remain resolvable only as historical citation targets until the new URL is captured. Initialization and synchronization recognize the descriptor as a capture request rather than ordinary prose. Fetching still requires the web approval described in Section 11.

Every active source, including natively searchable UTF-8 text, receives a content-versioned normalized representation under `sources/extracted/`. Native text conversion is a deterministic copy/normalization step; binary and structured sources use the selected allowlisted converter. This single invariant gives search and citations stable derivation paths with validated anchors instead of pointing them at mutable raw bytes.

The first release supports:

- Markdown and plain text.
- HTML.
- CSV and TSV.
- JSON.
- Text-readable PDF.
- DOCX.
- PPTX.
- XLSX.
- Common image formats through approved OCR or an agent's vision capability.
- Versioned webpage snapshots and downloaded web documents.

Unknown formats are recorded as `unsupported`; they are never silently ignored.

### 9.2 Logical sources, content versions, and derivations

A logical source has a stable generated ID. Ordinary file-backed sources have one or more content versions; an uncaptured URL descriptor is the sole exception and has zero versions until its first approved capture. Each content version is identified by SHA-256. A change in source bytes creates a content version; it does not overwrite the identity of older evidence. A web content version also retains an append-only sequence of retrieval events. Re-fetching identical bytes appends an event to the existing hash-keyed version; it does not duplicate the bytes or erase when, why, and under which approval they were retrieved.

Extraction identity is separate from source identity. Reprocessing unchanged bytes with a better parser or an approved newer agent recipe creates a new derivation revision. The record retains the extractor ID, extractor version, configuration digest, output path, output checksum, quality status, deterministic converter identity or immutable agent-handoff identity, and a short method note for each derivation. An agent-recipe revision is part of the approval-gated extractor configuration: changed agent output under an unchanged revision never overwrites or silently relabels an existing derivation.

Exact content-versioned extraction paths follow a predictable form such as:

```text
sources/raw/books/example.pdf
sources/extracted/books/example.pdf/<content-sha256>/<derivation-id>.md
```

Citations always identify this exact source version and derivation; a moving `latest` path is never the only citation target.

Agents must not modify an original in place. If a user modifies a previously ledgered raw path, synchronization marks it `integrity_error` and pauses adoption of the new bytes until the user chooses whether to restore the original or accept the bytes as a new content version.

Before adopting replacement bytes, the old exact bytes must remain resolvable. If they are available from the current Git history, the adoption workflow materializes them at `sources/raw/_versions/<source-id>/<content-sha256>/<original-name>` and rewrites old citation targets to that path. If the old bytes cannot be recovered, adoption remains blocked until the user restores or supplies them. The validator therefore never certifies a citation whose original path contains bytes different from the cited checksum.

`brain source adopt-version` is the only normal mechanism for accepting such replacement bytes. It requires a recorded approval, verifies the observed old and new checksums, materializes the old version before changing the active version, appends a durable adoption event, emits the exact old citation-path rewrite, and then routes the adopted bytes through normal extraction. It never attempts to infer approval from an edited working tree.

Removing an original never automatically deletes its ledger history, extracted representations, or dependent citations.

### 9.3 Sharded ledger

Each logical source has one JSON record under `sources/ledger/<source-id>.json`. At minimum it contains:

- Stable logical source ID.
- Current and previous raw paths.
- Detected media type and byte size.
- Content versions and SHA-256 values.
- Active version.
- Extraction derivations, checksums, and immutable deterministic/agent method provenance.
- Retrieval metadata for web sources.
- State and diagnostics.
- Creation, inspection, extraction, and update times.

Valid lifecycle states are:

- `pending`
- `extracting`
- `ok`
- `warning`
- `needs_agent`
- `failed`
- `unsupported`
- `integrity_error`
- `awaiting_approval`

`sources/ledger.md` is a generated summary, not a second source of ledger truth. It shows counts, gaps, last synchronization, corpus revision, and links to individual records and artifacts.

### 9.4 Corpus revision

The corpus revision is derived from the sorted set of active logical-source IDs, active content hashes, and selected extraction revisions. Question records store the revision used for their current answer.

## 10. Initialization and synchronization

### 10.1 Full initialization

Full initialization is an agent-orchestrated workflow. `brain init` performs and checkpoints the deterministic phase:

1. Inventory all content beneath `sources/raw/`.
2. Detect media types and create or reconcile source records.
3. Resolve each source through the approved registry.
4. Report missing dependencies and wait while the calling agent obtains approval.
5. Process the entire corpus through a bounded worker pool.
6. Checkpoint after every source, allowing an interrupted run to resume.
7. Route vision-dependent or semantically complex formats to `needs_agent` and emit a machine-readable handoff manifest.
8. Validate every deterministically produced representation.
9. Generate `sources/ledger.md` and the corpus revision.

The `brain-initialize` skill then drains the handoff manifest through the source-ingestion agent, registers each result with `brain source register-extraction`, and reruns validation. Full initialization succeeds only when the initial corpus has no unexplained pending work, `needs_agent` items, or silent gaps.

A person may run `./brain init` directly, but a bare CLI invocation cannot perform vision or other LLM work. When the handoff manifest is nonempty, it exits with clear instructions to invoke the `brain-initialize` skill. Running the command directly is equivalent to the full workflow only when every source has a deterministic extractor.

Independent sources continue after one extraction fails. A finished run may report `complete_with_gaps` and a non-success validation result while retaining all successful output.

Initialization does not generate wiki pages. The wiki grows from actual questions.

### 10.2 Incremental synchronization

`brain sync` runs before every substantive question.

The fast path compares source count, normalized paths, size, and modification metadata with ledger records. Count equality is only a hint; it is not proof of synchronization. New or suspicious candidates receive a SHA-256 check. `brain validate --full` rehashes every retained content version and derivation, not only the active pair, and is mandatory before completed handoff or PR readiness.

Synchronization handles:

- New paths.
- Changed bytes.
- Existing `pending` records whose prerequisites are now available.
- Existing `needs_agent` records that still lack a valid active representation.
- Retryable `failed` records when their input, extractor, configuration, or prerequisite state changed, or when the user explicitly requests a retry.
- Previously `unsupported` records when a newly approved registry entry supports them.
- Renames where identity can be established unambiguously.
- Missing paths.
- Missing or corrupt extracted output.
- A changed extractor version or configuration.
- Stale `extracting` states left by interruption.

Permanently repeating the same failed conversion before every question is forbidden. An unchanged failure remains visible as a coverage gap until a relevant condition changes or the user requests retry. A `warning` representation remains searchable but its limitation is disclosed.

Synchronization performs no network access. A newly discovered URL descriptor moves to `awaiting_approval` and is returned to the answer skill. The agent asks the user before calling `brain source snapshot-url`; approval resumes capture, extraction, and registration.

Synchronization processes every candidate that can currently become searchable, hands `needs_agent` work to the source-ingestion agent, checkpoints each result, regenerates summary state, and reports any remaining coverage gaps before query research begins.

### 10.3 Atomicity and locking

Extraction output and ledger records are written to temporary files, validated, and moved into place atomically. A repository-local lock prevents concurrent source writers. A stale lock is recoverable using recorded process and start metadata.

Wiki edits are validated as one logical change set. Git remains the recovery mechanism; the project does not introduce a second commit or session-management system.

## 11. Web source model

Public web access is a fallback, never an implicit extension of local search.

When repository evidence is insufficient, the agent must explain the gap and ask the user before browsing. Approval covers one clearly stated, bounded research event for the current question. The workflow assigns that event one approval event ID, which every capture made within the approved scope may reference; each retrieval still records its own timestamp and metadata. A later event, a refresh after the event ends, or a materially expanded scope requires a new approval and a new event ID.

After approval:

1. Search and inspect candidate public sources.
2. Distinguish sources actually used as evidence from pages merely viewed or returned as results.
3. Save every used source locally before using its claims in the durable answer or wiki.
4. Download PDFs, office documents, and other supported files beneath `sources/raw/_web/<source-id>/<content-sha256>/<filename>`.
5. Save webpages as immutable raw snapshots and create searchable Markdown representations.
6. Record requested URL, final URL, redirects, retrieval time in UTC, detected media type, checksum, and extraction details.
7. Return to the source-research stage and run exactly three `rg` passes over the updated active corpus before synthesizing again.

Network capture is restricted to public HTTP(S) destinations. The capture transport rejects credentials and every loopback, private, link-local, multicast, reserved, or otherwise non-public literal or resolved address; applies the same check to every redirect; caps redirect depth; and verifies the connected peer against the vetted resolution so DNS rebinding cannot turn public-web approval into internal-network access.

A URL refresh always appends an immutable retrieval event. If the bytes changed, it also creates a new content version and snapshot path; if the bytes are identical, it reuses the already verified snapshot and derivation for that checksum. It never overwrites the evidence or retrieval history used by an older citation. Search-result snippets and unused candidate pages are not added merely because they appeared during research.

If a static HTTP response does not faithfully represent a rendered page, an approved browser workflow must first save the rendered HTML, PDF, or other claim-bearing bytes into the repository's staging area and register those bytes as the immutable raw web snapshot. Derived Markdown alone is never accepted as the raw capture. If a page cannot be saved faithfully, its claims are not used as durable evidence. The agent may tell the user that the page existed but cannot treat it as grounded repository knowledge.

During initialization, user-provided URL descriptors are inventoried locally first. The agent then requests one clearly scoped batch approval before fetching them.

## 12. Question workflow

Every substantive knowledge question uses the following pipeline.

### 12.1 Synchronize

Run `brain sync`. The command returns the exact set of new active representations. The answer skill runs its initial discovery terms against that set before accepting an older wiki answer as sufficient. A relevant hit makes the wiki stale for this question and triggers the full three-pass source workflow. An unavailable new source creates a disclosed coverage gap.

### 12.2 Search the wiki first

The answering skill derives exact terms, named entities, aliases, dates, acronyms, spelling variants, and obvious phrases from the question. It searches `wiki/pages/` and `wiki/questions/` before the broader corpus.

### 12.3 Judge sufficiency

The wiki is satisfactory only when it:

- Directly addresses the user's intent.
- Covers all material parts of the question.
- Has resolving claim-level citations.
- Reflects the current relevant corpus.
- Contains no unresolved material contradiction.

If all conditions hold and no relevant source change exists, the system builds a validated wiki-evidence packet from the resolving citations and proceeds directly to Section 12.5. It skips source research, but it still creates or updates the topic's evolving Q&A record and validates the resulting graph before answering.

### 12.4 Run three source passes when insufficient

When the wiki is insufficient, exactly three `rg`-based source passes are mandatory:

1. **Discovery:** high-recall terms, names, aliases, acronyms, dates, spelling variants, and phrases derived from the question.
2. **Expansion:** entities, terminology, relationships, events, and contextual phrases discovered in pass one.
3. **Verification:** focused searches for decisive support, temporal qualifiers, exceptions, conflicting evidence, and counterexamples.

The LLM creates the keyword sets. `brain search` invokes `rg` over active ledger-selected representations. Literal pattern files are preferred for literal terms so punctuation cannot become unintended regular expressions.

Each logical pass first requests matching filenames and bounded context. A large result may be returned through resumable cursor pages bound to the same pass, terms, operands, and corpus revision; every page must be drained and the pass marked complete before synthesis. Cursor continuation does not create a fourth pass. An expired, stale, tampered, or abandoned search run is a disclosed blocking coverage gap, never silent truncation. Research agents then read the relevant sections or complete candidate documents. The workflow does not dump the entire corpus into model context. `rg` supplies multithreaded matching; the orchestrator avoids many competing full-tree processes that would only contend for disk I/O.

### 12.5 Synthesize and persist

The answer is assembled from an evidence map containing supporting passages, counterevidence, source IDs, versions, anchors, and uncertainty. Factual claims receive claim-level citations.

The workflow then:

1. Creates or updates the topic's question record.
2. Creates or updates qualifying wiki pages.
3. Searches existing pages for topics related to the question and adds reciprocal page-to-question and question-to-page links.
4. Reconciles links across candidate pages and existing question records.
5. Runs citation and graph validation.
6. Answers the user with any remaining uncertainty or coverage gaps disclosed.

If evidence is still insufficient, the record is marked partial or unanswered and the agent offers the approval-gated web fallback. After approved web evidence is captured and ledgered, control returns to Section 12.4: all three source passes run again against the updated corpus before a new synthesis is persisted.

## 13. Question records

A standalone question creates one topic-based Markdown record beneath `wiki/questions/`. Follow-up questions refine the same record instead of creating a file for every conversational turn.

Each record includes:

- Stable ID, title, and one-sentence description.
- Canonical current question.
- Earlier question phrasings and meaningful refinements.
- Current best answer.
- Answer status: `answered`, `partial`, `unanswered`, or `conflicted`.
- Corpus revision and last research date.
- Keyword sets used in each of the three source passes when applicable.
- Supporting and contradictory evidence.
- Related wiki pages.
- Claim-level citations and a `Sources` section.

Routine acknowledgements, repository-maintenance requests, and incidental conversational questions are not archived as knowledge questions.

## 14. Wiki page model

### 14.1 Qualification

A distinct person, organization, place, concept, event, project, decision, relationship, recurring question, or other reusable subject qualifies once there is at least one useful sourced statement about it.

The rule is intentionally permissive. Small pages are valid. Empty stubs, common dictionary words without corpus-specific meaning, and alias-only pages are not.

### 14.2 Required structure

Every page contains YAML frontmatter with:

- Stable page ID.
- Title.
- One-sentence description.
- Open-ended type.
- Aliases.
- Creation and update dates.

The visible Markdown contains:

- An explanatory title and concise summary.
- As many topic-specific sections as the evidence requires.
- Claim-level citation markers.
- `Related pages` with contextual relationship descriptions.
- `Related questions` linking relevant Q&A records.
- A final `Sources` section linking exact original and extracted evidence.

Standard relative Markdown links are canonical.

### 14.3 Citation contract

A factual passage cites the exact source version and useful location, such as page, slide, sheet, section, or deterministic block anchor. The corresponding source entry links:

- The exact original-version path: the current raw path only when its checksum still matches, otherwise a materialized `_versions/` path or immutable web snapshot.
- The exact extracted derivation.
- The source/version identifier and checksum.

Page-level source lists without claim mapping are insufficient for factual prose.

### 14.4 Link density

The first meaningful occurrence of a related topic in each section is linked. Repeated occurrences need not be. This interprets “fully interconnected” as complete relationship coverage rather than marking up every repeated word.

Self-links are forbidden. Ambiguous aliases are never replaced blindly.

## 15. Graph maintenance

When a page is created or materially changed:

1. Collect its title, aliases, entities, dates, and important phrases.
2. Use `rg` to search all existing pages and question records for candidate incoming relationships.
3. Read only the matching contexts.
4. Let the wiki-curator decide which candidates are genuine and unambiguous.
5. Add contextual inline links or `Related pages` entries.
6. Add relevant `Related questions` links and update those question records with reciprocal `Related pages` links.
7. Validate the complete graph.

When a question record is created or materially changed, the same process runs in reverse: `rg` finds relevant existing pages, the curator evaluates the contexts, and both sides receive reciprocal links. Declared page/question relationships are valid only when both directions resolve.

Candidate discovery can scan every page and question quickly without asking an LLM to reread the entire wiki. Actual edits use Markdown-aware logic; raw global string replacement is forbidden because it can corrupt frontmatter, code blocks, citations, existing links, substrings, and ambiguous names.

Candidate discovery is complete rather than top-N: if results require cursor pages, the curator drains every page tied to the current graph revision before publishing. An incomplete, expired, stale, or tampered candidate run blocks the transaction so alphabetically later inbound relationships cannot be silently omitted.

An unambiguous rename updates all inbound links and validation must pass. Deleting, merging, or materially splitting a page requires approval. After any approved removal of a page, claim, relationship, or other knowledge, `rg` locates every affected page and Q&A record; the curator reconciles prose, links, and citations, and validation verifies that no stale reference remains.

Contradictory evidence is retained, dated where relevant, and cited on both sides. The system does not silently overwrite an older supported claim. Choosing a materially disputed interpretation requires approval.

## 16. Skills and agent roles

### 16.1 Skills

- **`brain-initialize`:** orchestrates dependency preflight, approval, full extraction, agent-assisted formats, ledger completion, and validation.
- **`brain-answer`:** runs synchronization, wiki search, sufficiency judgment, mandatory source passes, synthesis, persistence, and validation.
- **`brain-web-research`:** explains the evidence gap, requests permission, researches, ingests used evidence, and returns to the mandatory three-pass source search before synthesis.
- **`brain-wiki-maintenance`:** applies page-qualification, citation, link, rename, removal, and contradiction rules.
- **`brain-validate`:** performs final integrity checks before handoff or PR review.

### 16.2 Agents

- **Source researcher:** read-only. Develops keyword sets, performs evidence searches, reads candidate material, and returns a structured evidence map.
- **Source ingester:** handles `needs_agent` items using vision or document judgment. It writes only to the exact source or browser staging path assigned by the CLI, then invokes the handoff or snapshot command; deterministic code alone validates and publishes raw web bytes, extracted representations, and ledger state.
- **Wiki curator:** owns semantic links and citations after evidence assembly. It writes proposed pages and Q&A records only to the assigned wiki-staging tree, then invokes the atomic wiki-apply command; it never edits the live wiki directly.
- **Brain auditor:** read-only. Reviews contradictions, citation coverage, corpus coverage, ledger consistency, and graph health.

Read-only researchers may work in parallel on independent evidence clusters. Ingestion finishes before source research. Only one curator owns a given wiki write set, preventing concurrent semantic edits to the same pages.

## 17. Hybrid approval model

### 17.1 Automatic operations

The system may automatically:

- Reconcile source inventory and checksums.
- Run already installed, allowlisted extractors.
- Add validated extraction output and ledger records.
- Use an agent to extract an image or complex document locally.
- Create an evidence-backed page.
- Improve an evolving Q&A answer while preserving meaningful history.
- Add claim-level citations and unambiguous links.
- Repair clearly broken links or formatting.
- Perform a clear spelling correction or unambiguous page rename when every link is repaired and validation passes.

### 17.2 Approval-gated operations

The system must ask before:

- Accessing the public web.
- Installing system software or repository-local packages.
- Adding or changing an extractor allowlist entry.
- Deleting, modifying, or moving an original source as a system action.
- Deleting, merging, or materially splitting wiki pages.
- Removing a sourced claim.
- Choosing between materially contradictory interpretations.
- Performing a major rewrite based on uncertain evidence.
- Proceeding with an ambiguous rename.

## 18. Error handling

- Missing tools leave affected items `pending` and produce an installation proposal.
- Corrupt, encrypted, empty, timed-out, truncated, or unsupported inputs receive durable states and diagnostics.
- Low-confidence extraction is `warning` or `needs_agent`, never silently `ok`.
- Independent files continue processing after one failure.
- A coverage gap produces a provisional answer with the unavailable source set identified.
- A webpage that cannot be saved locally cannot support a durable claim.
- A validation failure means the update is unfinished; the agent repairs it or reports the exact blocker.
- Stale extraction locks and `extracting` records are recoverable after an interrupted process.
- Missing originals do not trigger automatic deletion of derived evidence or citations.
- An in-place change to a ledgered original produces `integrity_error` until the user restores it or explicitly adopts it as a new content version.

## 19. Validation

`brain validate` is the deterministic gate before handoff or PR readiness. It checks:

- Every discovered source has a ledger record.
- Every active source has an allowed, explicit state.
- Active extraction outputs exist and match recorded checksums.
- Content and derivation versions are internally consistent.
- No generated directory is recursively ingested as a source.
- Raw originals were not silently modified by an agent.
- Citation targets, source IDs, versions, and anchors resolve.
- Relative Markdown links resolve.
- Every declared page-to-question relationship has a resolving reciprocal question-to-page relationship, and vice versa.
- Page and question IDs and slugs are unique.
- Self-links and unresolved ambiguous aliases are absent.
- Evidence-backed pages are not unexplained orphans.
- The human-readable ledger summary matches machine records.

Normal validation uses metadata and recorded hashes and runs during question workflows. `brain validate --full` rehashes every retained content version and derivation, including historical citation targets, and runs before completed handoff or PR readiness.

## 20. Testing strategy

### 20.1 Unit tests

Unit tests cover:

- Inventory inclusion and exclusion.
- Reserved sentinel and `_versions/` exclusion.
- MIME detection.
- Fast-path metadata comparison and candidate hashing.
- Logical source, content-version, and derivation identity.
- Registry selection and safe subprocess argument construction.
- Ledger state transitions and summary generation.
- Interrupted extraction recovery.
- Retry eligibility for pending, failed, unsupported, and `needs_agent` records.
- URL snapshot versioning.
- URL-descriptor approval state with zero network access during plain synchronization.
- Human and JSON command output.

### 20.2 Fixture integration tests

Small fixtures cover PDF, DOCX, PPTX, XLSX, image, HTML, CSV, malformed, encrypted, empty, and unsupported inputs. They verify location markers, nonempty output, checksum recording, resumability, and explicit failure reporting.

Core CI mocks optional external converters. Real-converter integration tests run when the relevant approved tools are installed.

### 20.3 Graph and citation tests

Tests cover broken links, missing citation targets, original-byte checksum mismatches, duplicate IDs, page renames, ambiguous aliases, reciprocal page/Q&A relationships, approved knowledge removal, code-block preservation, source replacements, historical source versions, and interrupted multi-file maintenance.

### 20.4 Scale test

A synthetic large corpus verifies that an unchanged `brain sync` follows the metadata fast path and does not re-open or rehash every source. It also verifies bounded output and memory use.

### 20.5 LLM workflow evaluations

LLM behavior uses versioned synthetic evaluation scenarios and normalized event logs instead of exact-text assertions. Final readiness requires an actual isolated execution of every scenario through both Codex and Claude; deterministic validators reject a missing client/scenario log or an event-order/policy violation. Scenario runs use no personal corpus and no live public network. They confirm that both clients:

- Search the wiki first.
- Persist or refine the topic Q&A record even on the wiki-sufficient fast path.
- Run all three source passes when the wiki is insufficient.
- Drain every continuation page within each logical pass before synthesis.
- Expand keywords from discovered context.
- Search for counterevidence.
- Cite exact source versions.
- Ask before browsing.
- Persist every external source actually used.
- Route extraction and rendered-browser handoffs through their distinct commands.
- Create appropriately atomic pages.
- Repair relevant links.
- Preserve contradictory evidence.
- Avoid archiving repository-maintenance conversations as knowledge questions.

## 21. User journey

The README documents this primary path:

1. Create a repository from the template, preferably private.
2. Add originals beneath `sources/raw/**`.
3. Ask Codex or Claude to “initialize this brain,” which invokes the `brain-initialize` skill and `./brain init`.
4. Review and approve proposed dependency installations and initial URL access.
5. Allow the resumable full initialization to finish.
6. Inspect `sources/ledger.md` for complete coverage and visible gaps.
7. Ask the first substantive knowledge question.
8. Review the grounded answer, persisted Q&A record, new or updated wiki pages, and citations.
9. Add new sources whenever desired; the next knowledge question synchronizes them automatically.
10. Use the normal branch, worktree, commit, PR, and merge workflow chosen by the user or host application.

The README also documents direct `./brain init` use. It explains that the command completes deterministic extraction but hands non-deterministic `needs_agent` items back to Codex or Claude; users with such items must invoke the initialization skill to finish.

The brain does not infer when a conversation ends. The desired history is one logical commit for a completed conversation or PR, normally produced by the user's host workflow or a squash merge. The repository neither auto-commits nor invents another session boundary. `brain validate --full` is required before a completed handoff.

## 22. Privacy and safety

The populated brain is expected to contain private and potentially sensitive data. The README prominently recommends a private repository and explains that:

- Git history retains committed content after ordinary deletion.
- Hosting-provider and organizational access controls remain the user's responsibility.
- Public-web approval applies to outbound research, not just downloading files.
- Source filenames and converter arguments are handled without shell interpolation.
- Symlinks escaping `sources/raw/` are not followed.
- Document macros and embedded executable content are never run.

## 23. Acceptance criteria

The design is successfully implemented when:

1. A fresh template contains no personal corpus or generated wiki content and passes validation.
2. Reserved empty-directory sentinels are never ingested as sources.
3. The committed extractor registry contains tested preferred/fallback strategies and install recipes for every core format.
4. The `brain-initialize` workflow completely and resumably processes the supported initial corpus, including `needs_agent` items, with all gaps visible.
5. `brain sync` detects and processes new, changed, renamed, missing, pending, newly supported, or stale sources without reconverting unchanged content or endlessly retrying unchanged failures.
6. A newly added URL descriptor causes no network access until the agent obtains approval.
7. The same canonical workflows are discoverable from Codex and Claude entrypoints without policy duplication.
8. A first question against an empty wiki runs three source passes, answers with exact citations, creates its evolving Q&A record, and creates qualifying linked pages.
9. A later question can answer from a satisfactory current wiki without unnecessary full-corpus research.
10. Insufficient local evidence triggers a permission request before any public-web access.
11. Every external source used in a durable answer is downloaded or snapshotted, extracted, ledgered, and cited locally, after which all three source passes rerun.
12. Creating or updating either a page or Q&A record searches the full wiki for candidates and leaves reciprocal valid relationships.
13. Every certified citation resolves to original bytes whose checksum matches the cited version, plus its exact extraction derivation.
14. Destructive, contradictory, uncertain, installation, allowlist, and web operations respect the hybrid approval rules.
15. Failures and partial coverage cannot be mistaken for successful complete ingestion.
16. Deterministic tests, workflow evaluations, and `brain validate --full` pass before handoff.

## 24. Implementation boundaries

Implementation should proceed in testable increments:

1. Repository contract, instruction routing, CLI skeleton, schemas, and validation foundation.
2. Source inventory, sharded ledger, versioning, registry, and synchronization.
3. Core document extractors, agent-assisted extraction, and web snapshots.
4. Wiki and Q&A schemas, `rg` search tooling, citations, and graph validation.
5. Shared skills, tool-specific agent adapters, workflow evaluations, and final README.

These are implementation milestones, not separate runtime services. The finished system remains one repository and one composable CLI plus agent workflows.
