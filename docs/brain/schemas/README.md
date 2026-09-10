# Second Brain Lite schemas

These files are the versioned JSON contracts for source-ledger records and
wiki Markdown frontmatter. A filename's `v1` suffix is part of the contract;
incompatible changes require a new version rather than silently widening the
existing schema. `question-frontmatter.v2.schema.json` is the canonical
QuestionRecord contract. It adds an intentionally typed contradictory-
interpretation decision; v1 remains historical documentation only.

`source-record.v1.schema.json` describes the canonical JSON form emitted by
`SourceRecord.to_dict()`. Raw paths are POSIX paths relative to `sources/raw`,
derivation output paths are repository-relative beneath `sources/extracted`,
and timestamps use UTC with a trailing `Z`. Retrieval events and version
adoption events are immutable audit history and must only be appended.

The page and question schemas describe frontmatter objects only. Markdown
headings, prose, wiki links, and citations are intentionally outside their
scope.
