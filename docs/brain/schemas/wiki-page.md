# Wiki page record

A page has frontmatter fields `id`, `title`, `description`, `type`, `aliases`,
`created`, and `updated`, with no unknown fields. `description` is a nonempty
single sentence and both dates are ISO `YYYY-MM-DD` values. Scalar values are
strings; `aliases` is an inline scalar list.

The body begins with one `#` title matching `title`, followed by a nonempty
`## Summary`, one or more topic sections, `## Related pages`, `## Related
questions`, and a final `## Sources`. Section headings are unique. Each
relationship is either absent or a line in the form
`- [Title](destination): One-sentence reason.`; malformed relationship lists
are rejected. Declared page and question relationships MUST be reciprocal
repository graph claims, not informal suggestions. The first meaningful visible
occurrence of a related topic within an eligible section MUST link to that
related record; a heading, code span, citation definition, or later mention
does not satisfy this requirement. Ambiguous aliases or relationship targets
MUST be reported as ambiguity; validators and curators MUST NOT auto-resolve
them.

Factual prose MUST use the citation-definition grammar under the final Sources
section. Normal validation checks metadata and graph relationships without new
content hashes; full validation differs only by additionally rehashing
referenced evidence files.
