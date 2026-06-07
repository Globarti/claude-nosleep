# Claude No-Sleep

> Close the lid. Keep the agent.

A macOS menu bar app that blocks system sleep **only while a Claude Code agent is actually doing work**. Walks away from the laptop, agent keeps running. Work ends, Mac sleeps like normal.

![Close the lid. Keep the agent.](docs/assets/hero-walking.jpg)

<sub>Photo: Roberto Hund / Pexels</sub>

**Full story:** [globarti.github.io/claude-nosleep](https://globarti.github.io/claude-nosleep/)

---

## Why this exists

Claude Code, Cursor, Codex CLI and friends take 5–30 minutes per turn. On Apple Silicon, closing the lid puts the Mac to sleep via a hardware magnet — `caffeinate` and `pmset disablesleep` normally don't override it. So vibe coders sit awkwardly next to open laptops waiting for agents to finish.

Existing tools (Amphetamine, KeepingYouAwake, Theine) are **manual switches** — you forget them on (battery dies) or off (agent dies). This one watches your actual workload and toggles automatically.

## How it works

- **launchd job** runs `claude-watch.py --apply` every 30 seconds.
- For every live `claude` process, classifies state via a 3-tier signal:
  1. **`~/.claude/sessions/<pid>.json` `status` field** — Claude Code writes `"busy"` / `"idle"` here on every tool-start, tool-end, and API-response-complete edge. Canonical, deterministic.
  2. **Wedge check** — if `status == "busy"` but the transcript jsonl hasn't been touched in 90 seconds, the session is stuck. Mark as wedged, allow sleep.
  3. **CPU fallback** — for old Claude Code versions (pre-2.1.158) that don't write the `status` field yet: CPU ≥ 2% = working.
- If any session is working → `sudo pmset -c/-b disablesleep 1`. Otherwise → `disablesleep 0`. Mac sleeps normally.
- **SwiftBar plugin** runs `claude-watch.py --status` every 10s for the UI: ⚡ green `N/total` count when working, 🌙 grey count when idle. The dropdown lists each session by its **chat name** (the AI-generated terminal-tab title, e.g. "Review D+D contract…"), with a second dim line of cwd / CPU / uptime / PID and an **instant submenu** showing that session's last 👤 USER prompt + 📝 RECAP (no slow hover tooltip). Each row is prefixed with a signal badge: ✓ (status), ⚠ (wedge), ☕ (caffeinate-child fallback).

```
┌─────────────────────────────────────────────────────────┐
│ launchd ──► claude-watch.py --apply (every 30s)         │
│             └─ any session busy → pmset disablesleep 1  │
│                else            → pmset disablesleep 0   │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ SwiftBar ──► claude-watch.py --status (every 10s)       │
│             └─ render menu, no pmset writes             │
└─────────────────────────────────────────────────────────┘
```

## Install

```bash
brew install --cask swiftbar jordanbaird-ice    # Ice fixes notch overflow

git clone https://github.com/Globarti/claude-nosleep.git
cd claude-nosleep
./install.sh                                     # copies files, loads launchd
open -a SwiftBar
```

`install.sh` is portable (uses `$HOME`, no hardcoded paths) and idempotent.
It prints the one manual step left: granting passwordless sudo for `pmset`.

<details><summary>…or do it by hand</summary>

```bash
mkdir -p ~/.claude/nosleep ~/SwiftBarPlugins
cp claude-watch.py focus-tty.sh ~/.claude/nosleep/
chmod +x ~/.claude/nosleep/{claude-watch.py,focus-tty.sh}

cp swiftbar-plugin.sh ~/SwiftBarPlugins/claude-nosleep.10s.sh
chmod +x ~/SwiftBarPlugins/claude-nosleep.10s.sh
defaults write com.ameba.SwiftBar PluginDirectory -string "$HOME/SwiftBarPlugins"

cp com.bartek.claude-nosleep.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bartek.claude-nosleep.plist

open -a SwiftBar
open -a Ice
```
</details>

Then grant passwordless sudo for `pmset` (the watcher can't toggle sleep without it):

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/pmset" | sudo tee /etc/sudoers.d/claude-nosleep
```

Verify:

```bash
pmset -g | grep SleepDisabled                  # 1 when something is working
~/.claude/nosleep/claude-watch.py --status     # raw plugin output
```

## Requirements

- macOS 13+ (Ventura or later)
- Claude Code installed
- NOPASSWD sudo for `pmset` (or full NOPASSWD: ALL)
- Python 3.9+ (stdlib only)

## Privacy

The watcher writes its state to `~/.claude/nosleep/state.json` with `0600`
permissions (owner-only) — it contains the last prompt + recap of each session,
so it deliberately stays out of world-readable `/tmp`. Nothing leaves your
machine; there is no network code.

## Lid-closed sleep

`pmset disablesleep 1` on `-c` (AC) does suppress clamshell sleep on Apple Silicon — `caffeinate` doesn't. The watcher sets both `-c` and `-b`, so it works on battery too. **Don't close the lid on battery while an agent is burning CPU** — it'll run until empty. (Yes, we found this out the hard way.)

## What it doesn't do

- Keep your **display** on (sleep is whole-system; the screen can dim freely).
- Watch Cursor / Aider / Codex CLI — only Claude Code processes. PRs welcome.
- Jiggle the mouse or simulate keypresses.
- Ship as a notarized DMG. Read the single ~650-line Python file.

## License

MIT.

## Made by

[Thebartglowacki@gmail.com](mailto:Thebartglowacki@gmail.com)
