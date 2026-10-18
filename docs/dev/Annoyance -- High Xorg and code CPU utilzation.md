my ubuntu workstation shows sporadically (daily) high CPU usage of Xorg and vs code up to 100%, currenty 50% without any activity in code (or the workstation). Closing/restart code solve the issue. How to fix
-->
The pattern — Xorg _and_ an Electron process both burning CPU with no input, cleared by restarting VS Code — is almost always a repaint storm: something inside VS Code is redrawing continuously, and every frame costs Xorg as much as it costs the client. On NVIDIA + X11 this is a well-worn failure mode.

## Narrow it down first (takes 2 min while it's happening)

```bash
# which VS Code process, and which thread inside it
top -H -p $(pgrep -d, -f '/usr/share/code')
# what Xorg is actually doing
sudo perf top -p $(pidof Xorg)
```

Then **Help → Open Process Explorer** in VS Code. It splits usage into `window`, `extensionHost`, `ptyHost`, `fileWatcher`, and `shared`. The label tells you which of the three fixes below applies:

- **`window` hot** → renderer repaint loop (GPU/compositing or an animating webview)
- **`ptyHost`/`window` together** → integrated terminal redraw
- **`fileWatcher` hot** → inotify storm
- **`extensionHost` hot** → a specific extension, and Xorg would normally be _idle_

If `perf top` on Xorg shows time in `nvidia_drv` / `glXWaitVideoSync` style symbols rather than damage processing, it's the GL spin-wait case (last section).

## Most likely culprit given your setup

A Claude Code CLI session left running in an integrated terminal. Its spinner animates indefinitely, xterm.js re-renders every frame, and with the WebGL renderer on the 1060 that goes straight through to Xorg. It survives until you kill the terminal — which is exactly what restarting VS Code does. Same story for any long-lived `tail -f`, `docker logs -f`, or a stuck progress bar in a background terminal tab.

Test it: when CPU is high, close terminal tabs one at a time and watch `top`.

Permanent mitigation in `settings.json`:

```jsonc
"terminal.integrated.gpuAcceleration": "off",
"terminal.integrated.smoothScrolling": false,
"terminal.integrated.scrollback": 5000,
```

## If it's the renderer, not the terminal

Disable Electron's hardware acceleration — this is the single highest-yield fix for NVIDIA/X11 Xorg spin. Edit `~/.config/Code/argv.json`:

```jsonc
{
  "disable-hardware-acceleration": true
}
```

Restart VS Code fully (`pkill -f /usr/share/code` first, the file is read at launch). If that fixes it but the UI feels sluggish, back off to `code --disable-gpu-compositing` instead.

Also cut the ambient animation sources:

```jsonc
"editor.cursorBlinking": "solid",
"editor.smoothScrolling": false,
"workbench.list.smoothScrolling": false,
"editor.minimap.enabled": false,
"window.autoDetectColorScheme": false
```

## If `fileWatcher` is hot

Very plausible with your git worktree layout — each worktree gets its own watch tree, and `.venv` plus `.git/objects` are murder on inotify.

```jsonc
"files.watcherExclude": {
  "**/.git/objects/**": true,
  "**/.git/subtree-cache/**": true,
  "**/.venv/**": true,
  "**/node_modules/**": true,
  "**/__pycache__/**": true,
  "**/.worktrees/**": true
},
"search.followSymlinks": false
```

And raise the ceiling so it fails loudly instead of spinning:

```bash
echo 'fs.inotify.max_user_watches=1048576' | sudo tee /etc/sysctl.d/60-inotify.conf
echo 'fs.inotify.max_user_instances=1024' | sudo tee -a /etc/sysctl.d/60-inotify.conf
sudo sysctl --system
```

## The NVIDIA spin-wait case

If `perf top` points into the driver, the classic fix is to stop the GL client from busy-spinning:

```bash
# ~/.profile or /etc/environment
export __GL_YIELD="USLEEP"
```

Given you already traced the EnterVT failures to `ForceFullCompositionPipeline`, double-check nothing has reintroduced it — `grep -ri 'CompositionPipeline' /etc/X11/`. `nvidia-settings` writes it back into `~/.nvidia-settings-rc` and into xorg.conf if you ever hit "Save to X Configuration File".

## Bisect if none of the above lands

```bash
code --disable-extensions
```

Run a normal day like that. If it stays clean, re-enable in halves. Webview-based extensions with a persistent panel are the usual offenders — a panel with a live spinner repaints forever even when collapsed.

My ordering of bets for your machine: integrated terminal first, `disable-hardware-acceleration` second, watchers third.


The vscode process explorer is clean. it was the terminal docker log and I can reproduce it by reopening
-->
That profile is conclusive, and it explains the thing that looked contradictory: Process Explorer shows every VS Code process at 0% while Xorg burns 50–100%. That asymmetry is the signature of the xterm.js WebGL/canvas renderer on the proprietary NVIDIA driver. Each batch of log lines costs the client almost nothing — a texture upload and a handful of GL calls — but the server-side glyph atlas work and full-viewport blit land entirely in `nvidia_drv.so`, which is where ~90% of your Xorg samples are sitting. `docker logs -f` on a chatty container feeds that loop continuously, and it keeps running whether or not the terminal tab is visible.

## Immediate fix

```jsonc
// settings.json
"terminal.integrated.gpuAcceleration": "off",
```

This switches xterm.js to the DOM renderer. Text throughput drops somewhat, but the per-frame cost stops going through the NVIDIA X driver. On your hardware this alone should take Xorg back to near-idle with the same `docker logs -f` running. Reload the window, then reproduce — it's a clean A/B.

Pair it with:

```jsonc
"terminal.integrated.scrollback": 5000,
```

10k default × several terminals is a lot of retained buffer for something you're going to `grep` anyway.

## Better: don't stream logs into the editor at all

The renderer fix treats the symptom. `docker logs -f` in an interactive terminal is a poor fit regardless — you can't search it, it dies with the window, and it's re-costing you every line.

```bash
# detached, survives VS Code restarts, attach only when you need it
tmux new -d -s dockerlogs 'docker logs --tail 1000 -f <container>'
tmux attach -t dockerlogs

# or land it in a file and read on demand
docker logs -f <container> >> /var/tmp/celex-<container>.log 2>&1 &
tail -n 200 /var/tmp/celex-<container>.log
```

If you do want it live in the terminal, cut the volume at the source so there's less to render:

```bash
docker logs --tail 1000 -f <container> 2>&1 | grep --line-buffered -Ev 'DEBUG|healthcheck'
```

## Also worth setting

Since Xorg is demonstrably spending its time inside the driver, the spin-wait mitigation is cheap insurance for the next variant of this:

```bash
# /etc/environment
__GL_YIELD="USLEEP"
```

Log out and back in for it to take effect.

One unrelated thing visible in your Process Explorer: the `docker events --since ... --until ...` under extension-host is the Docker extension re-polling on a 5-minute window. Harmless, but if you don't use its UI, disabling that extension trims a process and a poll loop.