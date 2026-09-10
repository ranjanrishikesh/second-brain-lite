---
name: brain-validate
description: Use before claiming a Second Brain handoff, completed conversation, or pull request is ready.
---

# Validate the brain

Read `docs/brain/workflows/validate.md` and run focused tests first. Then run:

```bash
python3 -m pytest -v
./brain --json validate --full
```

Inspect the structured report, not exit code alone. Completion requires no unexplained source gap, stale extraction, integrity error, unresolved citation or anchor, broken/one-way relationship, duplicate ID/slug, invalid skill link, or adapter drift. Warnings are acceptable only when their limitation is explicitly carried into the answer or handoff. This skill reports and verifies; it does not infer approval for destructive repair and never commits automatically.
