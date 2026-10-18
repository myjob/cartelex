

Those come from the **Tasks** community plugin. The `TQ_*` properties are its "Query File Defaults" feature (introduced in Tasks 7.15.0) — putting e.g. `TQ_short_mode: true` in a note's frontmatter injects `short mode` into every `tasks` code block in that file.

Why you see them without having added anything: the plugin automatically registers all ~22 of them in your vault's property types, i.e. in `.obsidian/types.json`. So they show up in the "Add property" suggester and the All Properties view even though zero notes use them. They don't touch your notes.

Getting rid of them:

1. **If you don't use Tasks**: disable/uninstall it, close Obsidian, then open `.obsidian/types.json` and delete the `"TQ_..."` lines from the `types` object. Deleting them from the Properties view UI doesn't stick — they come back on restart. Note that if you use Obsidian Sync, another device can push them back, so clean up after syncing everything.
2. **If you do use Tasks**: they'll be re-added on every plugin load. There's no setting to turn this off — it's open request [#3331](https://github.com/obsidian-tasks-group/obsidian-tasks/issues/3331), high priority but unresolved as of the 8.x line. The only real options are pinning Tasks to a pre-7.15 version, or living with the clutter.

Worth checking your community plugin list first — if Tasks isn't there, it was probably installed at some point and removed, and the types.json leftovers are all that remain.


Then something else put them there — Task Genius doesn't use the `TQ_` prefix. That exact set of names (`TQ_explain`, `TQ_extra_instructions`, `TQ_show_on_completion`, `TQ_show_toolbar`, …) is one-to-one the **Tasks** plugin's Query File Defaults list. Most likely explanations:

- Tasks is (or once was) installed in this vault — even a brief install leaves the property types behind in `.obsidian/types.json` forever
- the vault or its config was synced/copied from a device or template vault where Tasks ran

Two different things to check, because your screenshot looks like the **File properties** panel of one specific note rather than the vault-wide "All properties" view:

**1. If this note actually has them in its frontmatter** (that's what the checked `TQ_explain` and `Empty` value suggest) — open the note in source mode and delete the `TQ_*` lines from the YAML block at the top. Then grep the vault for stragglers:

```
rg -l "^TQ_" /path/to/vault
```

**2. The vault-wide registry** — close Obsidian, edit `.obsidian/types.json`, remove every `"TQ_...": "checkbox"` / `"text"` entry. Deleting them through the Properties UI won't hold; they reappear on restart. If Tasks is still enabled, it re-adds them on every load, so uninstall it first. And if you use Sync, clean up after all devices are in sync, otherwise one of them pushes the entries back.

Check `Settings → Community plugins` for "Tasks" — if it's listed there and you don't actually use it, uninstalling makes the fix permanent.