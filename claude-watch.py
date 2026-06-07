#!/usr/bin/env python3
"""
claude-watch: detect live Claude Code sessions, classify working vs idle,
toggle pmset disablesleep, and emit SwiftBar-friendly status.

Modes:
  --apply   (default in launchd) toggle pmset based on aggregate state
  --status  emit SwiftBar plugin output (also prints state info), no pmset writes
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
STATE_FILE = Path("/tmp/claude-nosleep-state.json")

# Activity thresholds
CPU_WORKING_THRESHOLD = 2.0    # %CPU above which a session is treated as "working" (legacy fallback)
WEDGE_STALE_S = 90             # status=busy + jsonl quiet >= this → session is wedged, treat as idle
RECENT_TOOL_USE_S = 20         # tool_use within last 20s → still "working" even if CPU drops
IDLE_DECAY_S = 60 * 60         # transcripts not touched in last hour → ignored

# Leading spinner / status glyphs Claude Code prepends to the terminal tab title
# (✳ and the braille spinner frames). Stripped so the row shows just the chat name.
_SPINNER_RE = re.compile(r"^[\s⠀-⣿✳-❇·••·∙]+")


def active_terminal_ttys():
    """Return set of TTY paths currently held by visible terminal windows.

    Supports Terminal.app and iTerm2. Returns None if no terminal emulator
    responded (caller should then assume all claude processes are active).
    """
    ttys = set()
    saw_any = False
    # Terminal.app
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "Terminal" to get tty of every tab of every window'],
            capture_output=True, text=True, timeout=2
        )
        if out.returncode == 0 and out.stdout.strip():
            saw_any = True
            for chunk in out.stdout.split(","):
                t = chunk.strip()
                if t.startswith("/dev/tty"):
                    ttys.add(t)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # iTerm2
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "iTerm2" to get tty of current session of every tab of every window'],
            capture_output=True, text=True, timeout=2
        )
        if out.returncode == 0 and out.stdout.strip():
            saw_any = True
            for chunk in out.stdout.split(","):
                t = chunk.strip()
                if t.startswith("/dev/tty"):
                    ttys.add(t)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ttys if saw_any else None


def terminal_titles():
    """Return {tty: chat_name} from the Terminal/iTerm2 tab titles.

    Claude Code sets each terminal tab's title to an AI-generated summary of the
    conversation ("Review D+D contract…", "listmonk-twenty-crm-integration", …) —
    this *is* the chat name the user sees. We map it by TTY, which we already key
    every session on, and strip the leading spinner/status glyph.
    """
    titles = {}
    script = (
        'tell application "Terminal"\n'
        '  set out to ""\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      try\n'
        '        set out to out & (tty of t) & "\\t" & (custom title of t) & "\\n"\n'
        '      end try\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if "\t" not in line:
                    continue
                tty, name = line.split("\t", 1)
                tty = tty.strip()
                name = strip_spinner(name)
                if tty.startswith("/dev/tty") and name:
                    titles[tty] = name
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return titles


def strip_spinner(name: str) -> str:
    """Strip the leading spinner/status glyph Claude Code prepends to tab titles."""
    return _SPINNER_RE.sub("", name or "").strip()


def get_pid_tty(pid: int):
    try:
        out = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(pid)], text=True,
            stderr=subprocess.DEVNULL
        ).strip()
        if not out or out == "?" or out == "??":
            return None
        return f"/dev/{out}"
    except subprocess.CalledProcessError:
        return None


def list_claude_pids():
    """Return list of (pid, cwd, tty) for live `claude` CLI processes
    attached to a TTY that belongs to an actually-open terminal window."""
    active_ttys = active_terminal_ttys()
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,command="], text=True
        )
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if not m:
            continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "claude-watch" in cmd or "claude-status" in cmd:
            continue
        if "Claude.app" in cmd or "Google Chrome" in cmd:
            continue
        if not re.search(r"(^|/)claude\b", cmd.split()[0]):
            continue
        tty = get_pid_tty(pid)
        # Filter: TTY must belong to a currently-open terminal tab.
        # If we couldn't query any terminal emulator (active_ttys is None),
        # fall back to counting all claude processes.
        if active_ttys is not None and (tty is None or tty not in active_ttys):
            continue
        cwd = get_cwd(pid)
        pids.append((pid, cwd, tty))
    return pids


def get_cwd(pid: int):
    try:
        out = subprocess.check_output(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:]
    except subprocess.CalledProcessError:
        pass
    return None


def cwd_to_project_dir(cwd: str | None):
    if not cwd:
        return None
    # Claude encodes cwd as -Users-foo-bar
    return PROJECTS_DIR / ("-" + cwd.replace("/", "-").lstrip("-"))


def latest_jsonl(project_dir: Path):
    if not project_dir or not project_dir.is_dir():
        return None
    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    return candidates[0] if candidates else None


def session_jsonl_for_pid(pid: int, cwd: str | None):
    """Resolve the *specific* jsonl transcript for a given claude PID by
    reading ~/.claude/sessions/<PID>.json (sessionId → jsonl filename).
    Returns None if no session file exists or the jsonl is missing.
    """
    sess_file = SESSIONS_DIR / f"{pid}.json"
    if not sess_file.is_file():
        return None
    try:
        data = json.loads(sess_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    session_id = data.get("sessionId")
    sess_cwd = data.get("cwd") or cwd
    if not session_id or not sess_cwd:
        return None
    proj_dir = cwd_to_project_dir(sess_cwd)
    if not proj_dir:
        return None
    jsonl = proj_dir / f"{session_id}.jsonl"
    return jsonl if jsonl.is_file() else None


def tail_last_n(path: Path, n: int = 30):
    """Return last n non-empty lines as parsed JSON dicts."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # ~3KB per event is a generous upper bound for jsonl entries
            chunk_size = min(size, max(64 * 1024, n * 3 * 1024))
            f.seek(-chunk_size, 2)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [l for l in data.splitlines() if l.strip()]
    parsed = []
    for line in lines[-n:]:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def get_pid_cpu(pid: int) -> float:
    """Instantaneous %CPU for the given pid. 0.0 on failure."""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "%cpu="], text=True,
            stderr=subprocess.DEVNULL
        ).strip()
        return float(out) if out else 0.0
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def get_pid_etime(pid: int) -> str:
    """Process elapsed time as short human string like '4h', '12d', '23m'."""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "etime="], text=True,
            stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        return ""
    # etime formats: MM:SS, HH:MM:SS, DD-HH:MM:SS
    if "-" in out:
        days = int(out.split("-", 1)[0])
        return f"{days}d"
    parts = out.split(":")
    if len(parts) == 3:
        return f"{int(parts[0])}h"
    if len(parts) == 2:
        m = int(parts[0])
        if m >= 60:
            return f"{m // 60}h"
        return f"{m}m" if m else "<1m"
    return out


def read_session_status(pid: int):
    """Return Claude Code's self-reported status ("busy" / "idle") for a PID,
    or None if the session file is missing or doesn't have the field
    (older Claude Code versions before ~2.1.158 don't write it).

    This is the canonical signal — Claude Code itself uses these files as the
    inter-process activity channel, so it's more reliable than CPU sampling.
    """
    sess_file = SESSIONS_DIR / f"{pid}.json"
    if not sess_file.is_file():
        return None
    try:
        data = json.loads(sess_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("status")


def has_caffeinate_child(pid: int) -> bool:
    """Claude Code spawns `caffeinate -i -t 300` as a direct child whenever
    it's actively processing a turn (the child dies/respawns around tool-edges).
    Presence of this child is a near-perfect "working now" signal that works
    even on older Claude Code versions that don't write the status field.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid), "-lf", "caffeinate"],
            text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return False
    return bool(out.strip())


def classify_state(pid: int, jsonl_path: Path):
    """Return (state, last_text, age_s, cpu, last_prompt, last_recap, signal_src).

    Signal precedence (most → least deterministic):
      1. `~/.claude/sessions/<pid>.json` `status` field (Claude Code's own
         busy/idle flag). If "busy", cross-check the jsonl mtime — a busy
         session whose transcript has been silent > WEDGE_STALE_S is wedged
         (hung tool, stuck loop) and is treated as idle so the Mac can sleep.
      2. CPU% per PID (legacy fallback for older Claude Code versions that
         don't write the status field).

    signal_src is "status" / "wedge" / "cpu" — used in the UI for transparency.
    """
    cpu = get_pid_cpu(pid)
    age = None
    last_kind = ""
    last_prompt = ""
    last_recap = ""

    if jsonl_path and jsonl_path.is_file():
        age = time.time() - jsonl_path.stat().st_mtime
        events = tail_last_n(jsonl_path, 300)
        if events:
            last_kind = describe_event(events[-1])
        last_prompt = extract_last_user_prompt(events)
        last_recap = extract_last_recap(events)

    status = read_session_status(pid)
    if status == "busy":
        if age is not None and age > WEDGE_STALE_S:
            return ("wedged", last_kind, age, cpu, last_prompt, last_recap, "wedge")
        return ("working", last_kind, age, cpu, last_prompt, last_recap, "status")
    if status == "idle":
        return ("idle", last_kind, age, cpu, last_prompt, last_recap, "status")

    # No status field (old Claude Code). Use Claude's own caffeinate child
    # as the working signal — it's spawned only during active turns and
    # disappears when idle. Far cleaner than CPU sampling.
    if has_caffeinate_child(pid):
        return ("working", last_kind, age, cpu, last_prompt, last_recap, "caffeinate")
    return ("idle", last_kind, age, cpu, last_prompt, last_recap, "caffeinate")


def extract_last_user_prompt(events) -> str:
    """Get most recent user prompt. Uses the dedicated `last-prompt` event
    type that Claude Code writes after every user turn. Falls back to scanning
    user messages if no last-prompt event is present.
    """
    for ev in reversed(events):
        if ev.get("type") == "last-prompt":
            txt = (ev.get("lastPrompt") or "").strip()
            if txt:
                return txt
    # Fallback: walk user messages
    for ev in reversed(events):
        msg = ev.get("message", {}) or {}
        if (msg.get("role") or ev.get("type", "")) != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            txt = content.strip()
            if txt and not txt.startswith("<") and not txt.startswith("[tool"):
                return txt
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    txt = (c.get("text") or "").strip()
                    if txt:
                        return txt
    return ""


def extract_last_recap(events) -> str:
    """Return the most recent recap (Claude Code stores these as system
    events with subtype='away_summary'). Strips the trailing
    "(disable recaps in /config)" hint Claude appends to every recap.
    """
    for ev in reversed(events):
        if ev.get("type") != "system":
            continue
        if ev.get("subtype") != "away_summary":
            continue
        content = ev.get("content") or ""
        if not isinstance(content, str):
            continue
        # Strip the trailing config hint Claude appends.
        text = re.sub(r"\s*\(disable recaps in /config\)\s*$", "", content).strip()
        if text:
            return text
    return ""


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def describe_event(ev):
    msg = ev.get("message", {}) or {}
    role = msg.get("role") or ev.get("type", "")
    content = msg.get("content", "")
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "tool_use":
                    return f"[{role}] tool:{c.get('name', '?')}"
                if c.get("type") == "text":
                    txt = c.get("text", "")[:60]
                    return f"[{role}] {txt}"
        return f"[{role}] (multi)"
    if isinstance(content, str):
        return f"[{role}] {content[:60]}"
    return f"[{role}]"


def collect():
    """Return list of dicts describing each live session."""
    titles = terminal_titles()
    sessions = []
    for pid, cwd, tty in list_claude_pids():
        # Prefer the per-PID session mapping (canonical). Fall back to the
        # latest jsonl in the cwd if the session file is missing.
        jsonl = session_jsonl_for_pid(pid, cwd) or latest_jsonl(cwd_to_project_dir(cwd))
        state, last, age, cpu, last_prompt, last_recap, signal_src = classify_state(pid, jsonl)
        # Chat name from the terminal tab title (the AI summary the user sees).
        # Untitled tabs read as "Claude Code" — disambiguate those by cwd basename.
        name = titles.get(tty or "", "")
        if not name or name.lower() == "claude code":
            base = os.path.basename((cwd or "").rstrip("/")) or "session"
            name = f"~/{base}"
        sessions.append({
            "pid": pid,
            "tty": tty,
            "name": name,
            "cwd": cwd or "?",
            "transcript": str(jsonl) if jsonl else None,
            "state": state,
            "signal": signal_src,
            "last": last,
            "age_s": int(age) if age is not None else None,
            "cpu": round(cpu, 1),
            "etime": get_pid_etime(pid),
            "last_prompt": last_prompt,
            "last_recap": last_recap,
        })
    return sessions


def apply_pmset(should_block: bool):
    """Set pmset disablesleep on both AC and battery."""
    target = "1" if should_block else "0"
    # check current state to avoid unnecessary sudo writes
    try:
        out = subprocess.check_output(["pmset", "-g"], text=True)
        m = re.search(r"SleepDisabled\s+(\d)", out)
        current = m.group(1) if m else None
    except subprocess.CalledProcessError:
        current = None
    if current == target:
        return False
    subprocess.run(
        ["sudo", "-n", "pmset", "-c", "disablesleep", target],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    subprocess.run(
        ["sudo", "-n", "pmset", "-b", "disablesleep", target],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return True


def write_state(sessions, blocking):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "blocking": blocking,
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def emit_swiftbar(sessions, blocking):
    working = [s for s in sessions if s["state"] == "working"]
    idle = [s for s in sessions if s["state"] == "idle"]
    n_total = len(sessions)
    n_work = len(working)

    # Menu bar title — compact count. ⚡ N/total when working, 🌙 total when idle.
    if n_total == 0:
        sf = "moon.zzz.fill"
        title = "0"
        color = "#8E8E93"
    elif n_work > 0:
        sf = "bolt.fill"
        title = f"{n_work}/{n_total}"
        color = "#34C759"
    else:
        sf = "moon.stars.fill"
        title = f"{n_total}"
        color = "#8E8E93"

    print(f"{title} | sfimage={sf} sfcolor={color} sfsize=16 size=13 color={color}")
    print("---")
    if blocking:
        print(f"Mac stays awake (sleep blocked) | sfimage=lock.fill sfcolor=#FF3B30")
    else:
        print(f"Mac will sleep normally | sfimage=lock.open sfcolor=#8E8E93")
    print(f"{n_work} working · {len(idle)} idle · {len(sessions)} total | sfimage=terminal.fill")
    print("---")

    if not sessions:
        print("No Claude sessions running | color=gray")
    for s in sessions:
        sf_session = {
            "working": "bolt.fill",
            "idle": "moon.stars.fill",
            "wedged": "exclamationmark.triangle.fill",
            "dormant": "moon.zzz.fill",
        }.get(s["state"], "questionmark.circle")
        sf_color = {
            "working": "#34C759",
            "idle": "#8E8E93",
            "wedged": "#FF9500",  # macOS system orange
            "dormant": "#48484A",
        }.get(s["state"], "#8E8E93")
        cwd_short = s["cwd"].replace(str(Path.home()), "~") if s["cwd"] else "?"
        cwd_short = cwd_short[-50:]
        uptime = s.get("etime") or "—"
        cpu_str = f"{s['cpu']}%" if s.get('cpu') is not None else "—"
        # signal source badge — show which signal decided the state
        sig = s.get("signal", "")
        sig_label = {"status": "✓", "wedge": "⚠", "caffeinate": "☕"}.get(sig, "")
        focus_action = ""
        if s.get("tty"):
            focus_action = f" bash={Path.home()}/.claude/nosleep/focus-tty.sh param1={s['tty']} terminal=false"

        # USER prompt + RECAP, independent 400-char caps per section.
        def _cap(text: str, n: int = 400) -> str:
            text = text.strip().replace("|", "¦")
            return text if len(text) <= n else text[: n - 1] + "…"

        prompt = _cap(s.get("last_prompt") or "")
        recap = _cap(s.get("last_recap") or "")

        # Keep the tooltip too (hover), but the real fix for the 1.5s tooltip
        # delay is the submenu below — it expands instantly on hover.
        tooltip_attr = ""
        if prompt or recap:
            parts = []
            if prompt:
                parts.append(f"👤 USER\n{prompt}")
            if recap:
                parts.append(f"📝 RECAP\n{recap}")
            tooltip_attr = f" tooltip={chr(10).join(parts)!r}"

        # Row label = chat name, truncated to keep the menu narrow. The full
        # name is the first submenu line (macOS menus can't marquee a row).
        name = s.get("name") or f"pid {s['pid']}"
        NAME_MAX = 40
        disp = name if len(name) <= NAME_MAX else name[: NAME_MAX - 1] + "…"
        print(f"{sig_label} {disp} | sfimage={sf_session} sfcolor={sf_color} font=Menlo size=13{focus_action}{tooltip_attr}")
        # Submenu: full chat name first (only when the row was truncated)…
        if len(name) > NAME_MAX:
            for i, ln in enumerate(textwrap.wrap(name, 52) or [name]):
                if ln.startswith("-"):
                    ln = " " + ln
                head = "💬 " if i == 0 else "   "
                print(f"--{head}{ln} | font=Menlo size=12")
            print("-----")
        # …then the dimmed metadata that used to be the whole row.
        print(f"--{cwd_short} · {cpu_str} · up {uptime} · pid {s['pid']} | font=Menlo size=10 color=gray")
        # Explicit focus item — reliable even if clicking the parent only
        # expands this submenu instead of firing its bash action.
        if focus_action:
            print(f"--→ Focus this tab | sfimage=arrow.right.circle{focus_action}")
        # Instant USER/RECAP submenu (no OS tooltip delay). No indentation —
        # only guard the rare wrapped line that *starts* with '-' (which SwiftBar
        # would otherwise read as extra menu nesting) by prefixing a NBSP.
        def _wrap(label, text, swatch):
            print(f"--{label} | font=Menlo size=11 color={swatch}")
            for ln in textwrap.wrap(text, 52) or [""]:
                if ln.startswith("-"):
                    ln = " " + ln
                print(f"--{ln} | font=Menlo size=11")
        if prompt:
            _wrap("👤 USER", prompt, "#34C759")
        if prompt and recap:
            print("-----")
        if recap:
            _wrap("📝 RECAP", recap, "#62ADFF")

    print("---")
    print(f"Refresh now | refresh=true sfimage=arrow.clockwise")
    print(f"Open state JSON | bash=open param1={STATE_FILE} terminal=false sfimage=doc.text")
    print(f"Open watcher log | bash=open param1=/tmp/claude-nosleep.log terminal=false sfimage=text.alignleft")
    print("---")
    print("About Claude No-Sleep | sfimage=info.circle")
    print("--Blocks Mac sleep while Claude Code sessions are working. | font=Menlo size=11")
    print("--made by Thebartglowacki@gmail.com | font=Menlo size=11 color=gray")
    print(f"--Watcher: ~/.claude/nosleep/claude-watch.py | font=Menlo size=10 color=gray")
    print(f"--Threshold: CPU ≥ 2% per claude process | font=Menlo size=10 color=gray")
    print(f"--Polling: every 30s (launchd) | font=Menlo size=10 color=gray")
    print("-----")
    print(f"--Open watcher source | bash=open param1={Path.home()}/.claude/nosleep/claude-watch.py terminal=false")
    print(f"--Open plugins folder | bash=open param1={Path.home()}/SwiftBarPlugins terminal=false")


def main():
    mode = "apply"
    if "--status" in sys.argv:
        mode = "status"

    try:
        sessions = collect()
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        # Anything unexpected: assume nothing is working so the Mac keeps its
        # default sleep behaviour rather than draining the battery flat.
        if mode == "apply":
            apply_pmset(False)
        sys.stderr.write(f"claude-watch: collect failed: {exc!r}\n")
        sys.exit(0)

    blocking = any(s["state"] == "working" for s in sessions)

    if mode == "apply":
        apply_pmset(blocking)
        write_state(sessions, blocking)
    else:
        write_state(sessions, blocking)
        emit_swiftbar(sessions, blocking)


if __name__ == "__main__":
    main()
