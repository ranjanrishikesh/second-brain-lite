# Onboarding, saving, and returning

This is agent-facing guidance. Run repository commands for the user and describe
the result in plain language. Read `BRAIN.md` for the source and question
lifecycle. Greeting and navigation requests do not create Q&A records.

## Greet and inspect

For "hi", "hello", "help me get started", or "what still needs attention",
run `./brain --json status` and inspect its JSON, including warnings and errors.
This command inventories sources without ingesting or changing them. If it
cannot run, explain the actual setup error and use `./brain --json doctor`
when available; ask before installing dependencies. Do not infer an empty
brain from a failed command, missing `.brain/`, or an empty wiki.

Use the reported state and a read-only inspection of `sources/raw/` and the
ledger when needed. Ignore reserved history/capture directories and sentinels
under the source-handling policy; do not count them as new user files.

| Observed state | Brief response and next step |
| --- | --- |
| Pristine template, no originals | Welcome the user; ask them to add original files to `sources/raw/`, then say "Initialize this brain." Give two or three source examples. |
| Originals present but not ingested | Say the files are present and offer "Initialize this brain." A status coverage gap is not proof that the folder is empty. |
| Previously initialized but empty | Explain that setup exists but there are no sources to answer from; invite files. |
| Existing brain with usable sources | Welcome them back and invite their next question. Report any relevant gaps without restarting initialization. |
| Pending, failed, approval-gated, or interrupted work | Name the reported issue and offer to resume initialization or take the specific recovery step. Preserve successful work. |

A greeting alone does not authorize ingestion, web access, dependency
installation, wiki edits, or Git writes. If the same message includes an
explicit action or question, route that request rather than stopping at a
welcome. Do not repeatedly onboard an established brain.

## Initialize, add material, and ask

- "Initialize this brain", "initialize brain", or "resume initialization":
  follow `brain-initialize`. If there are no originals, explain the empty
  state and where to add files; never imply useful knowledge was created.
- "I added more files; update my brain": follow the resumable initialization
  workflow; preserve processed sources and process only eligible additions.
- A substantive question, including one asked before explicit initialization:
  follow `brain-answer` and its synchronization/ingestion steps. Source files
  may already exist. Let the workflow establish coverage before answering;
  do not fabricate an answer or require redundant manual setup.
- "Add this link": follow `brain-web-research` and its bounded approval and
  capture workflow. A URL is a candidate, not permission to browse. The agent
  handles the URL descriptor or approved ad-hoc capture; users need not write
  descriptor metadata. For a link-only request, the local gap is the requested
  page not yet captured. Keep approval scoped to that page, complete the
  capture and local verification, and report its ingestion state without
  inventing a knowledge question or Q&A record.
- Unsupported input, insufficient evidence, or a failed conversion: explain
  what was usable, what remains unresolved, and a concrete next step. Keep
  approval gates and faithful extraction requirements intact.

Initialization does not generate wiki knowledge. Questions use the existing
grounded persistence workflow. User-facing format examples must agree with
`config/extractors.toml`; converter recipes remain agent-facing.

## Save and return

After initialization or meaningful source/wiki changes, briefly mention:
"When you're ready, say 'Save this work to main' so the next conversation can
use these changes." Do not repeat this after every small follow-up. A greeting
or unchanged answer needs no save reminder.

An explicit save/commit/push/merge request authorizes the corresponding Git
workflow for the current brain. Inspect `git status`, the current branch,
diffs, configured remotes, and the remote default branch before writing.
Default to `main` when it is the repository's branch; otherwise use the actual
default branch and name it to the user. Read `brain-validate` for the relevant
handoff gate. Normal brain usage requires full integrity and coverage checks;
pytest is an implementation/PR gate, not a per-conversation user operation.

Review and commit the relevant originals, immutable source versions,
extractions, machine/human ledger records, and wiki changes together so cited
evidence travels with the knowledge. Exclude temporary `.brain/` files,
credentials, unrelated edits, and implementation-only transcripts. Preserve
user work; do not stage everything blindly. Git provider file-size limits and
symlink support still apply (Windows may need Developer Mode or WSL).

Fetch the destination before publishing. Push directly when allowed, or use
the environment's branch/PR merge workflow and repository protections. Never
force-push, bypass protections, or resolve a meaning-changing conflict without
the necessary user decision. Verify the saved commit is contained in the
remote default branch; a local commit, branch push, or open PR is not a merge.
If access or a merge is pending, say so and preserve the local result.

For a fresh conversation, the user opens the latest remote default branch.
When asked to update the checkout, inspect local changes first; fetch and
fast-forward only where safe. Preserve dirty or diverged work and explain any
needed choice. Do not automatically switch branches on every question or
greeting. On the current checkout, use the normal question workflow, and
disclose any known unsaved or stale state. Knowledge is carried by repository
files, not by an assumed memory of previous conversations.
