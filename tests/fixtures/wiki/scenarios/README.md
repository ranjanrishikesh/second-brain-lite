# Knowledge scenario overlays

Each `scenarios/<name>/repo/` directory is a complete, internally consistent
repository overlay. It contains committed content in `sources/raw`,
`sources/extracted`, `sources/ledger`, `sources/ledger.md`, `wiki/pages`,
`wiki/questions`, and `wiki/index.md`; every referenced ledger shard and
summary row exists, with checksums, metadata, paths, and navigable anchor IDs
matching the committed bytes.

Canonical extracted fixtures preserve the complete raw basename:
`sources/extracted/<raw-parent>/<raw-name>/<content-sha256>/<derivation-id>.md`.
For example, a raw `notes/a.txt` is extracted beneath
`sources/extracted/notes/a.txt/<sha>/<derivation>.md`.

Broken scenarios contain exactly the one defect named by the test. Tests use
`scenario_repo` to install exactly one overlay, ensuring incompatible broken
states do not leak into another validation or transaction.
