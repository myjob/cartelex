 `2026-08-26
## What the name has to carry

The repo is not "a RAG over legal PDFs." Its distinguishing feature — visible in `fact_source_registry`, `fact_provenance()`, the `derived_from`/`gated_by` lineage edges, and the deterministic-first evidence verifier — is that **every extracted fact is traceable back to the paragraph it came from, including facts derived from other facts**. The extraction targets are also very specific: fines per company, leniency applicants vs. grants, infringement articles under treaty renumbering, geographic and product markets, NACE sector, infringement start/end dates.

So the name should say _structured, auditable facts from EU competition decisions_ — not "AI legal assistant."

## Why `celex-facts` is a weak public name

Three separate problems:

1. **CELEX is already taken in NLP.** The [CELEX Lexical Database](https://catalog.ldc.upenn.edu/LDC96L14) (Nijmegen/LDC) is a well-known corpus. Anyone in the ML half of your audience searching "celex" lands there, not on you.
2. **It's opaque to everyone else.** Outside EUR-Lex regulars, "CELEX" carries no meaning — and it looks like a pharma brand (Celexa).
3. **It names the input format, not the product.** CELEX is an identifier scheme you happen to key on. In five years you may ingest national competition authority decisions or the Russo book chapters (you already do the latter), and the name goes stale.

## Shortlist

|Name|Reads as|PyPI|Notes|
|---|---|---|---|
|**cartelex**|cartel + lex|free|Instantly legible, one word, no collisions found|
|**cartel-facts**|minimal rename of what you have|free|Zero explanation needed; keeps continuity with `celex-facts`|
|**recital**|the numbered paragraph a fact cites|**taken**|Most elegant; insiders get it immediately|
|**dossier**|the Commission case file|**taken**|Right metaphor, crowded namespace|

**My recommendation: `cartelex`.** It survives being said out loud once, the PyPI namespace and GitHub search are clear, and it's the only candidate that reads correctly to both audiences — a competition economist sees "cartel," a lawyer sees "lex."

**One caveat worth your call:** your fact spec covers abuse of dominance and Article 102 material, not only cartels. If you intend the public scope to be EU competition decisions generally, `cartelex` under-sells it and **`recital`** becomes the better pick — it names the unit of evidence rather than the offence type, which is exactly the thing your lineage system is built around. `recital` is taken on PyPI but that only forces a distinct distribution name (`recital-eu`); the GitHub repo name is unaffected.

`★ Insight ─────────────────────────────────────`

- **Repo name and package name are separate namespaces.** GitHub repo names are per-account (`youruser/recital` is free even if a `recital` package exists on PyPI), and your `pyproject.toml` already separates distribution name from the `src/` layout — so a rename touches `[project].name` and the `.egg-info`, not your import paths.
- **Renaming a GitHub repo is safe:** GitHub permanently redirects the old URL, `git remote` URLs, and clone paths. The one thing that does _not_ redirect is a repo later created at the old name — so if you rename, don't reuse `celex-facts`.
- **Keep `celex` as a domain term inside the code.** `celex_id`, the `CELEX-` prefix convention, and `celex_facts` as the Postgres schema are all correct and precise — the jargon problem is only at the shop window, not in the plumbing.`─────────────────────────────────────────────────`

I checked PyPI directly (results above are live HTTP status codes). The `gh search repos` calls returned empty from this sandbox — likely no network auth for the GitHub API here — so treat "no GitHub collisions" as unverified; worth a manual search before you commit to one.