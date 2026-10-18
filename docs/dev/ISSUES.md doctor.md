 `2026-09-12



[issue_md_doctor.py](src/tools/issue_md_doctor.py)

Usage (from project root):
  

```
.venv/bin/python src/tools/issue_md_doctor.py

.venv/bin/python src/tools/issue_md_doctor.py --dry-run

.venv/bin/python src/tools/issue_md_doctor.py --path docs/OTHER_ISSUES.md
```


result
```
ISSUES.md: no closed items left in 'Open Issues'

Open issues: 240
  #highest       4
  #high        126
  #med          64
  #low          22
  (untagged)    24
Closed issues: 127
```


**One real defect surfaced from a test, not from review.** My first version appended a blank separator line after each relocated block; when the closed chapter was empty that blank landed at EOF and grew the file's trailing whitespace on every run — breaking idempotence. The fix trims trailing blanks only when the source didn't end blank, so repeated runs are now byte-stable. `test_pass_is_idempotent` locks that in.

**Why `--dry-run` isn't a nicety here.** Because an item silently owns everything beneath it, a large block is exactly where this tool could do surprising damage. The dry run prints the line count of each block it would move, so a 27-line move gets a human glance before it happens.