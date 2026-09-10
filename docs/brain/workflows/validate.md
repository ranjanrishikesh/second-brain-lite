# Validation workflow

Read `BRAIN.md` and the policies/workflows relevant to the changed paths.

1. Run focused tests for changed behavior.
2. Run `python3 -m pytest -v`.
3. Run `./brain --json validate --full`.
4. Inspect state counts, coverage gaps, citation failures, and graph failures rather than relying only on exit status.
5. Do not claim completion while any unexplained pending, `needs_agent`, integrity, citation, or graph failure remains.
