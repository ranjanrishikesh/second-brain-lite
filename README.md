# Second Brain Lite

Turn your documents into a personal knowledge base you can ask questions about in Codex or Claude Code, with answers linked to your sources.

## Start your brain

1. Click **Use this template → Create a new repository** on GitHub.
2. Choose an owner and repository name, select **Private**, leave **Include all branches** unchecked, and click **Create repository from template**.
3. Open **your new repository** in Codex or Claude Code. For a local app, clone it using your Git app and open that folder; for a connected environment, select your new repository.
4. Say **“Hi”** for guidance, or add your files to `sources/raw/` and say **“Initialize this brain.”**

The agent checks setup, processes your sources, and tells you what is ready or needs attention. If the folder is empty, it helps you get started. It asks before installing missing tools or accessing the public web.

## What can I add?

Put original files in `sources/raw/`. Subfolders are welcome.

- **Notes and meeting transcripts:** Markdown or plain text.
- **Reports and documents:** PDF or Word (`.docx`).
- **Presentations:** PowerPoint (`.pptx`).
- **Spreadsheets and data exports:** Excel (`.xlsx`), CSV, TSV, JSON, or JSONL.
- **Saved pages and images:** HTML, PNG, JPEG, TIFF, or WebP, including screenshots and scans.
- **Web links:** Tell the agent “Add this link to my brain” and provide the URL.

Some formats need extra tools or agent assistance; the agent explains any limits. Leave the reserved `_versions/` and `_web/` folders alone.

## Use it through conversation

- **“What do my notes say about Project Alpha?”** — get a source-backed answer; useful findings are saved in the brain.
- **“I added more files. Update my brain.”** — process the additions. You can also ask your next question directly.
- **“What still needs attention?”** — check setup or processing gaps.
- **“Resume initialization.”** — continue interrupted setup.

The agent maintains the sources and wiki and explains when your material cannot answer a question.

## Save your work and come back

1. After initialization or a useful conversation, say **“Save this work to main.”** The agent checks the changes and commits and pushes them, or helps merge the conversation branch into `main`.
2. Wait for confirmation that the changes reached your repository’s `main` (or its default branch).
3. Start a fresh conversation from the **latest main**, then ask your next question. For a local checkout, let the agent update it first.

Your brain remembers through saved source and wiki files; a new conversation does not need the previous chat history.

Keep your repository private. Your agent provider may process the files you use, and deleting a file does not erase earlier copies from Git history.
