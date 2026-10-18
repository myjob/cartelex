The short answer is **yes, but with a major catch.** GitHub uses standard Markdown, while Obsidian uses some custom shorthand (like `[Wikilinks](Wikilinks)`) and has a flexible way of finding files. If you just push your Obsidian vault to GitHub as-is, **some of your links will likely break** on GitHub's web interface.


## Quick reference

|Goal|Obsidian-only|Portable (GitHub + Obsidian)|
|---|---|---|
|Link file|`[[plan]]`|`[plan](docs/claude/plans/plan.md)`|
|Link + alias|`[[plan\|the plan]]`|`[the plan](docs/claude/plans/plan.md)`|
|Heading|`[[plan#Setup]]`|`[setup](docs/.../plan.md#setup)`|
|Embed image|`![[diagram.png]]`|`![diagram](assets/diagram.png)`|
|Space in name|`[[Master Planner Agent]]`|`[Master Planner](docs/Master%20Planner%20Agent.md)`|

Set the two Obsidian settings in rule 1 once, and Obsidian will produce GitHub-ready links going forward.



Here is a breakdown of why they break and exactly how to fix it so your links work perfectly in both apps.

## Why Obsidian Links Break on GitHub

1. **Wikilinks vs. Standard Markdown:** By default, Obsidian uses `[Note Name](Note%20Name)`. GitHub does not recognize this format; it just displays it as plain text. GitHub requires standard Markdown links: `[Note Name](path/to/Note-Name.md)`.
    
2. **Missing `.md` Extensions:** Obsidian hides the `.md` extension in links. GitHub needs the literal filename, including `.md`.
    
3. **Path Resolution:** Obsidian is smart—if you link to `[Cooking](Cooking)`, it searches your entire vault and finds it. GitHub is strict—it needs the exact relative file path (e.g., `../hobbies/cooking.md`).
    

## How to Make Them Work Seamlessly

You don't have to give up Obsidian's ease of use to make your notes GitHub-compatible. You just need to tweak a few settings in Obsidian.

### Step 1: Change Obsidian's Link Settings (Recommended)

You can force Obsidian to write links in a standard format that GitHub natively understands.

1. Open Obsidian and go to **Settings** > **Files and links**.
    
2. **Turn OFF** "Use [Wikilinks](Wikilinks)". (This forces Obsidian to use standard `[Link](path)` syntax).
    
3. Change **New link format** to **Relative path to file**.
    

> **Note:** This will apply to all _new_ links you create. Obsidian will automatically format them as `[Note](capsule/Note.md)`, which works perfectly on both Obsidian and GitHub.

### Step 2: Convert Your Existing Links

If you already have hundreds of `[Wikilinks](Wikilinks)`, you don't need to fix them manually. You can use an Obsidian community plugin to convert them all at once.

1. In Obsidian, go to **Settings** > **Community plugins** > **Browse**.
    
2. Search for **Consistent Attachments and Links** or **Wikilinks to MDLinks**.
    
3. Install and enable the plugin.
    
4. Run the plugin's command to convert your vault's `[Wikilinks](Wikilinks)` into standard Markdown links.
    

## Quick Summary of Link Compatibility

|**Link Style**|**Works in Obsidian?**|**Works on GitHub?**|
|---|---|---|
|`[My Note](My%20Note)`|Yes|No|
|`[My Note](My-Note)`|Yes|No (Missing `.md`)|
|`[My Note](relative/path/to/My-Note.md)`|**Yes**|**Yes**|