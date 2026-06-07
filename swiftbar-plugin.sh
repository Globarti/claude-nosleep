#!/bin/bash
# SwiftBar plugin: shows Claude session status + sleep-block state in menu bar.
# Refresh every 10s. The launchd job com.bartek.claude-nosleep handles the
# actual pmset toggle every 30s — this script just displays.
# Portable: resolves the watcher via $HOME, no hardcoded user path.
exec "$HOME/.claude/nosleep/claude-watch.py" --status
