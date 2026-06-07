#!/bin/bash
# SwiftBar plugin: shows Claude session status + sleep-block state in menu bar.
# Refresh every 10s. The heavy detection (osascript/lsof) only re-runs every ~8s — between
# ticks claude-watch.py re-renders from its cached state, so 2s is cheap.
# The launchd job com.bartek.claude-nosleep handles the actual pmset toggle.
exec /Users/bartlomiejglowacki/.claude/nosleep/claude-watch.py --status
