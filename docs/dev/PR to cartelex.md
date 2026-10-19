 `2026-10-18

PR with cartelex

Written for: you, working in this repo (currently clean, on `main`).

**Option 1: GitHub Pull Requests extension (recommended)**
1. Install **GitHub Pull Requests** (`GitHub.vscode-pull-request-github`) and sign in to GitHub when prompted.
2. Create a branch off `main`. Don't commit to `main` directly. Use the branch name in the status bar's bottom-left, or run `Git: Create Branch...` from the Command Palette (`Ctrl+Shift+P`).
3. Commit your changes in the Source Control view (`Ctrl+Shift+G`), then click **Publish Branch** to push it.
4. Open the **GitHub** icon in the activity bar and click **Create Pull Request**. You can also run `GitHub Pull Requests: Create Pull Request` from the Command Palette.
5. Choose the base branch (`main`), fill in the title and description, and click **Create**.

**Option 2: Source Control view shortcut**
After you publish a branch, VS Code often shows a **Create Pull Request** button or notification. It opens the same form.

**Option 3: Terminal with `gh`**
```bash
git switch -c my-feature
git push -u origin my-feature
gh pr create --base main --fill
```

I can also create the branch, commit, push and open the PR from here. That would be outward-facing, so I'd confirm with you first. Say so if you want it.