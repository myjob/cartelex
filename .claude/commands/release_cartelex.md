---
description: Release the public cartelex tree - prepend release notes to RELEASE.md, make docs/ names Windows-safe, then run scripts/release_cartelex.sh
argument-hint: "[--dry-run] [VERSION] [TARGET_DIR]"
allowed-tools: Bash(git log:*), Bash(git diff:*), Bash(git status:*), Bash(git tag:*), Bash(git shortlog:*), Bash(.venv/bin/python src/tools/md_name_cleanser.py:*), Bash(scripts/release_cartelex.sh:*), Read, Edit
---

Release celex-facts to the public cartelex clone. Arguments: `$ARGUMENTS`
(`--dry-run` stops after the rsync preview; `VERSION` such as `v0.2.2`;
`TARGET_DIR` is passed through to the script). Work through the steps in
order and stop at the first failure.

## 1. Release notes -> top of RELEASE.md

1. Read the top entry of `RELEASE.md`. Its `**Period:**` line ends with the
   date of the previous release; everything committed after that date is new.
   If the top entry already covers today's date (notes written earlier today),
   show it and ask whether to rewrite or keep it.
2. Collect the changes since that date:
   - `git log --since=<date> --no-merges --format='%h %ad %s' --date=short`
   - `git diff --shortstat $(git rev-list -1 --before=<date> HEAD) HEAD`
   - read the closed `- [x]` items in `ISSUES.md` that the commits cite (`#NNN`)
     and the design docs under `docs/` the commits touch, to describe what
     changed for a user rather than repeating commit subjects.
3. If no VERSION was given, propose the next patch version and ask.
4. Insert the new entry ABOVE the previous one, in the same shape as the
   existing entries:
   - `# vX.Y.Z — <title>`, then `**Period:**` (previous date (version) to
     today) and `**Scope:**` (commits, files changed, +/- lines)
   - a short prose intro relative to the previous release, `---`
   - `## Highlights` (bullets), `## New features` (tables and bullets per
     area), then fixes, breaking/config changes and known issues where the
     commits give material for them
   - cite issue numbers as `#NNN` and design docs as `%20`-encoded links
   - no emoji; wrap placeholders such as `<date>` in backticks
5. Never edit older entries. Show the new entry to the user.

## 2. Markdown file-name check

Run the `/md_name_cleanser` procedure on `docs/`:
1. `.venv/bin/python src/tools/md_name_cleanser.py --dry-run`
2. On any CONFLICT line stop and ask the user which file keeps the name.
3. Otherwise apply (`.venv/bin/python src/tools/md_name_cleanser.py`), then
   gate with `.venv/bin/python src/tools/md_name_cleanser.py --check`
   (must exit 0). Relay renames and rewritten link files.

## 3. Commit gate

`scripts/release_cartelex.sh` builds from `git archive HEAD`, so uncommitted
release notes and renames would NOT be published.
1. Show `git status --short`.
2. If `RELEASE.md` or anything under `docs/` is uncommitted, ask the user to
   commit (offer `/commit`); do not commit without their go-ahead. Do not
   continue until the tree the user wants released is committed.

## 4. Build the public tree

1. Always preview first: `scripts/release_cartelex.sh --dry-run [TARGET_DIR]`.
   Relay the rsync changes (new / deleted / modified files) and every
   leak-scan WARNING line verbatim; a leak hit means a path should go into
   `PRUNE` or a `sub()` call in the script - suggest which.
2. If `--dry-run` was passed, stop here.
3. Otherwise ask for confirmation, then run `scripts/release_cartelex.sh [TARGET_DIR]`.
4. The script never commits or pushes. Finish by printing the follow-up the
   user runs themselves:
   `cd ~/a/cartelex && git status && git add -A && git commit -m "release vX.Y.Z" && git push`
   and, for a GitHub release, `gh release create vX.Y.Z --notes-file <notes>`
   with the new RELEASE.md entry as the notes.
