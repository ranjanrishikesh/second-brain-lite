# Question record

Question records MUST be one evolving record per topic, not a transcript per
conversation turn. Their frontmatter has `id`, `title`, `description`,
`canonical_question`, `prior_phrasings`, `answer_status`, `corpus_revision`,
`last_researched`, `discovery_terms`, `expansion_terms`, and
`verification_terms`, with no unknown fields. `description` is a nonempty
single sentence, `last_researched` is an ISO date, and `corpus_revision` is
exactly 64 lowercase hexadecimal characters. The corpus revision from the
completed source sync MUST be persisted in the question record. The answer status is exactly one
of `answered`, `partial`, `unanswered`, or `conflicted`.

The body has one matching `#` title and unique `## Current answer`, `##
Supporting evidence`, `## Contradictory evidence`, `## Related pages`, and a
final `## Sources` section. Related-page lists use
`- [Title](destination): One-sentence reason.`. Source sync MUST precede
substantive research. Approved web evidence MUST restart discovery, expansion,
and verification rather than being appended to a prior answer.

Every factual claim MUST use adjacent citation markers whose definitions use
the citation-definition grammar and appear in the final Sources section.
Declared page/question relationships MUST be reciprocal. Ambiguous links are
reported, never auto-resolved.

Canonical records use frontmatter v2. `not_applicable` has no companion fields
and may be answered, partial, or unanswered. `unresolved` is conflicted, has
no companion fields, and requires two distinct exact citations in `##
Contradictory evidence`. `preferred` is answered or partial, contains a local
selected citation ID and approval event ID, and cites that choice in both
Contradictory evidence and Current answer. Prose never proves or clears this
typed state; v1 is historical only.
