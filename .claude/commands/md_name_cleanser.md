---
description: Before a release, rename files under docs/ to Windows-acceptable names and rewrite the Markdown links that point at them
allowed-tools: Bash(.venv/bin/python src/tools/md_name_cleanser.py:*), Bash(git status:*)
---

Make the Obsidian vault in `docs/` cloneable on Windows, then report.

1. Preview: `.venv/bin/python src/tools/md_name_cleanser.py --dry-run $ARGUMENTS`
   - Each renamed note gets its old file name as its `#` top heading.
   - If it prints `All names are Windows-safe.` and no CONFLICT line, report
     that and stop.
2. If any CONFLICT is listed (a sanitized name collides with an existing
   file, or two names differ only in case), do NOT apply. Show the conflicts
   and ask the user which file should keep the name; the tool writes nothing
   while conflicts exist.
3. Otherwise apply: `.venv/bin/python src/tools/md_name_cleanser.py $ARGUMENTS`
   - Skip this step if the user asked for a preview only (`--dry-run`).
4. Confirm with the release gate: `.venv/bin/python src/tools/md_name_cleanser.py --check`
   (exit 0 means every name is safe).
5. Relay each rename (old -> new), each Markdown file whose links were
   rewritten, and the warnings (non-ASCII or near-MAX_PATH names are legal on
   Windows and are not renamed). Show `git status --short docs` so the user
   can stage the renames; do not commit.

Do not rename files by hand: the tool rewrites wikilinks (`[[name]]`,
`[[name|alias]]`, `[[name#heading]]`), `%20`-encoded Markdown links and raw
backtick paths in every `.md` file of the project together with the rename.
