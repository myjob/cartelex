
There's a built-in setting for exactly this — no plugin needed.

**Settings → Files and links → Default location for new attachments** → choose **"In subfolder under current folder"**. A **Subfolder name** field appears right below; type `images` (or `attachments`, `_assets`, whatever you like).

From then on, anything you paste into `Notes/Project/foo.md` lands in `Notes/Project/images/`.

A few things worth knowing:

- **Turn on "Automatically update internal links"** in the same settings pane. Then you can drag existing attachments into an `images` subfolder from the file explorer and Obsidian rewrites the links for you.
- **Use Wikilinks** (`![[image.png]]`) rather than Markdown links if you can — they resolve by filename regardless of path, so moving files around never breaks them. Markdown-style links (`![](images/foo.png)`) are path-dependent and only survive a move because of the auto-update setting.
- **Cleaning up existing messes**: for a whole vault, the community plugin **Attachment Management** (or **Custom Attachment Location**) can bulk-rearrange existing attachments and rename them to something like `note-name-1.png`. Both also let you use variables like `${notename}` in the folder path if you ever want more elaborate schemes.
- **Watch out for**: unused images left behind after you delete an embed. The **Find Orphaned Files** plugin catches those.

The one downside of per-folder subfolders vs. a single vault-wide `attachments/` folder: if you move a note between folders, its images stay behind. If you reorganize notes often, the vault-wide option ("In the folder specified below" → `images`) is more robust, at the cost of one big flat folder.