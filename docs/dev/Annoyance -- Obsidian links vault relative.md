

Obsidian resolves any non-`file://` link relative to the **vault root**, not the filesystem root. So `~/a/celex-facts/docs/infra/...` is being read as `<vault>~/a/celex-facts/docs/infra/...`, which doesn't exist → new note.

Quick diagnostic: in the note, type `[[INSTALL` and see if autocomplete offers the file.

**If it does** — the file is in the vault. Take the completion and let Obsidian write the path; strip the machine path entirely. If your vault root is `~/a/celex-facts`, the markdown-link equivalent is:

```markdown
[INSTALL celex-facts](docs/infra/INSTALL%20celex-facts.md)
```

No leading slash, path relative to vault root (or relative to the current note, depending on your _Files & Links → New link format_ setting).

**If it doesn't** — the file is outside the vault, and no internal link will ever reach it. You need a file URI, which hands off to your OS rather than opening in Obsidian:

```markdown
[INSTALL celex-facts](file://~/a/celex-facts/docs/infra/INSTALL%20celex-facts.md)
```

Percent-encoding is required here; angle brackets alone won't do it for `file://`.

Note that `%20` only works in markdown-style links — inside `[[wikilinks]]` you write the literal space.