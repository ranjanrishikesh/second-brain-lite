# Citation policy

## Immutable citation identity
Every factual claim maps to `source_id`, `content_sha256`, `derivation_id`, and a useful anchor. The original link resolves to bytes whose SHA-256 is `content_sha256`; the extraction link resolves to the recorded derivation.

## Placement
Put the citation marker directly after the smallest supported factual passage. A page-level bibliography alone is not claim-level provenance.

## Sources
Define every marker once in the final `## Sources` section using the grammar enforced by `./brain --json validate`. Link both the immutable original version and exact extracted representation.

## Unavailable evidence
Never cite a search-result snippet, an uncaptured webpage, an unresolved anchor, or a moving raw path whose bytes no longer match. Report the coverage gap instead.
