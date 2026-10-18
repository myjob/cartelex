 `2026-08-26
A separate "snapshot" repo is a recognized pattern for exactly this case (research code released for citation, no community expected). The failure mode isn't the two repos, it's treating the public one as a _fork_ that you then hand-edit. Treat it as a generated artifact and it's fine.

That said, for a paper reference, a bare GitHub repo is a weak citation target. Repos get renamed, moved, or deleted, and the URL in your paper dies. The standard fix is a DOI.

**What I'd actually do**

1. Keep developing in the private `celex-facts` repo.
2. Create `celex-facts-release` (public), populated by a scripted export, not by hand:
    
    ```
    git archive --format=tar v0.1.0 | tar -x -C ../celex-facts-release/
    ```
    
    Then in that directory: one commit, `v0.1.0` tag, push, cut a GitHub Release. No dev history travels with it.
3. Connect the public repo to Zenodo. Every GitHub Release then gets archived with a version DOI, plus a "concept DOI" that always resolves to the newest version. Cite the version DOI in the paper.

Step 2 is the real argument for the second repo, more than tidiness: your dev history almost certainly has connection strings, `.env` leftovers, absolute paths, maybe a DB dump. `git filter-repo` on an existing repo is error-prone; a squashed export leaks nothing by construction. Run `gitleaks detect` on the export anyway before pushing.

**If you'd rather do one repo**

Just make `celex-facts` public as-is. Messy dev docs genuinely don't matter — nobody reads them, and a `docs/dev/` folder is not a reputational problem. But then you're accepting the history-leak audit as your job, and you can't undo publication.

**Either way, the housekeeping that actually matters**

- **License.** Without one it's all-rights-reserved, which means "available for reference" is legally false. MIT or Apache-2.0 for code.
- **`CITATION.cff`** at the repo root. GitHub renders a "Cite this repository" button and it saves reviewers guessing.
- **README status block:** research code accompanying paper X, released for reference, no support, no contributions. That sets expectations better than any policy setting.
- **Turn off** Issues, Discussions, Wiki, Projects in repo settings. Forking you can't disable on a personal public repo — that's inherent to public. Accept it.
- **Don't ship the corpus.** Ship schema DDL, ingest scripts, and a pointer to EUR-Lex/GLEIF. Redistribution terms for the 740 documents and the 5M-row LEI table are a headache you don't need, and reviewers care about reproducible _ingestion_, not that you mirrored the data.

**The one thing to skip:** don't use an orphan branch in the private repo as the "public" branch. To be visible the repo has to be public, which exposes every other branch too — you get the worst of both.

The two-repo split costs you one scripted export per release. If you only ever cut one release for one paper, that's a ten-minute cost, once.