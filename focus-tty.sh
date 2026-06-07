#!/bin/bash
# Focus the Terminal.app (or iTerm2) tab whose tty matches $1.
# Usage: focus-tty.sh /dev/ttys040
TTY="$1"
# Strict whitelist — only allow /dev/ttysNNN. Refuses anything with quotes,
# shell metacharacters, or path traversal so the value is safe to interpolate
# into the AppleScript below.
if ! [[ "$TTY" =~ ^/dev/ttys[0-9]+$ ]]; then
  echo "focus-tty: refusing invalid TTY '$TTY'" >&2
  exit 1
fi

osascript <<EOF 2>/dev/null
tell application "Terminal"
  activate
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      try
        if tty of t is "$TTY" then
          set selected of t to true
          set index of w to 1
          set found to true
          exit repeat
        end if
      end try
    end repeat
    if found then exit repeat
  end repeat
end tell
EOF

# Fallback: iTerm2
if [ $? -ne 0 ]; then
  osascript <<EOF 2>/dev/null
tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if tty of s is "$TTY" then
            tell w to select t
            tell t to select s
            return
          end if
        end try
      end repeat
    end repeat
  end repeat
end tell
EOF
fi
