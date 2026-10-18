---
description: Tidy ISSUES.md - number open items, move closed items into the Closed Issues chapter and report open issues per severity
allowed-tools: Bash(.venv/bin/python src/tools/issue_md_doctor.py:*)
---

Run the issues-file doctor and report the result to the user.

1. Run: `.venv/bin/python src/tools/issue_md_doctor.py $ARGUMENTS`
   - Pass `--dry-run` through if the user asked to preview only.
   - The tool is deterministic; do not edit `ISSUES.md` by hand.
2. Relay its output: how many open items were numbered (and the number span
   they took), how many closed items moved (with the line count each block
   carried), the open-issue tally per severity, and the total the closed
   chapter now holds.
3. If it moved anything, mention that the moved block includes every
   continuation line under the item, so the user can sanity-check a large
   block before committing.
4. Issue numbers sit between the checkbox and the severity tag and are
   write-once: they are assigned in reverse document order, so a hand-added
   item on top of the chapter takes the next free number and no existing
   number ever shifts. Never renumber by hand -- a citation to an issue
   number is expected to stay valid.
