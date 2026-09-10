# Validation workflow

Read `BRAIN.md` and the policies/workflows relevant to the changed paths.

1. For repository implementation/PR work, run focused tests for changed behavior.
2. For the complete implementation gate, run `python3 -m pytest -v`, unless
   the user explicitly selected a scoped test plan; report that scope. Normal
   initialization, knowledge conversations, and saving their results do not
   require pytest. Inspect `./brain --json status` for their coverage gate.
3. Run `./brain --json validate --full`.
4. Inspect state counts, coverage gaps, citation failures, and graph failures rather than relying only on exit status.
5. Do not claim completion while any unexplained pending, `needs_agent`, integrity, citation, or graph failure remains.
