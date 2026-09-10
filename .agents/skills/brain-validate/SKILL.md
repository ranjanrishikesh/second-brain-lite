---
name: brain-validate
description: Use before claiming a Second Brain handoff, completed conversation, or pull request is ready.
---

# Validate the brain

Read `docs/brain/workflows/validate.md`. For normal brain initialization,
question answering, and saving knowledge, run the full integrity check and
inspect a current init/sync/status report for coverage gaps:

```bash
./brain --json status
./brain --json validate --full
```

For repository implementation or PR work, run focused tests first. The default
complete implementation gate is below; follow an explicitly agreed scoped
test plan when the user has selected one, and report that scope accurately:

```bash
python3 -m pytest -v
./brain --json validate --full
```

Do not run pytest for each normal user conversation or source addition.

Inspect the structured report, not exit code alone. Completion requires no unexplained source gap, stale extraction, integrity error, unresolved citation or anchor, broken/one-way relationship, duplicate ID/slug, invalid skill link, or adapter drift. Warnings are acceptable only when their limitation is explicitly carried into the answer or handoff. This skill reports and verifies; it does not infer approval for destructive repair and never commits automatically.
