#!/bin/bash
# Claude No-Sleep installer. Portable — no hardcoded paths, uses $HOME.
# Re-runnable (idempotent). Run from the repo root:  ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOSLEEP="$HOME/.claude/nosleep"
PLUGINS="$HOME/SwiftBarPlugins"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="com.bartek.claude-nosleep.plist"

echo "▸ Installing watcher to $NOSLEEP"
mkdir -p "$NOSLEEP" "$PLUGINS" "$AGENTS"
cp "$REPO/claude-watch.py" "$REPO/focus-tty.sh" "$NOSLEEP/"
chmod +x "$NOSLEEP/claude-watch.py" "$NOSLEEP/focus-tty.sh"

echo "▸ Installing SwiftBar plugin (10s refresh)"
cp "$REPO/swiftbar-plugin.sh" "$PLUGINS/claude-nosleep.10s.sh"
chmod +x "$PLUGINS/claude-nosleep.10s.sh"
defaults write com.ameba.SwiftBar PluginDirectory -string "$PLUGINS" 2>/dev/null || true

echo "▸ Installing launchd job (toggles pmset every 30s)"
cp "$REPO/$PLIST" "$AGENTS/$PLIST"
launchctl unload "$AGENTS/$PLIST" 2>/dev/null || true
launchctl load "$AGENTS/$PLIST"

echo
echo "✓ Installed."
echo
echo "One thing left: the watcher needs passwordless sudo for pmset."
echo "Add this with  sudo visudo  (replace \$USER if editing by hand):"
echo
echo "    $USER ALL=(root) NOPASSWD: /usr/bin/pmset"
echo
echo "Then start SwiftBar:  open -a SwiftBar"
echo "(Optional, fixes the notch hiding the icon:  brew install --cask jordanbaird-ice && open -a Ice)"
echo
echo "Verify:  pmset -g | grep SleepDisabled   # 1 while an agent is working"
