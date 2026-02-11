#!/usr/bin/env python3
"""
Niri window focus tracker for correlating EEG data with user activity.

Subscribes to niri's event stream and logs every focus change with timestamps
that align with the Muse S EEG recordings.

Output: muse_data/session_TIMESTAMP/focus.csv

Usage:
    python focus_tracker.py                          # log to latest session
    python focus_tracker.py --session muse_data/session_20260210_172929
    python focus_tracker.py --standalone             # log to new standalone file
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time


def get_latest_session(data_dir="muse_data"):
    """Find the most recent session directory."""
    if not os.path.isdir(data_dir):
        return None
    sessions = sorted(
        [d for d in os.listdir(data_dir) if d.startswith("session_")],
        reverse=True,
    )
    return os.path.join(data_dir, sessions[0]) if sessions else None


def categorize_window(title: str, app_id: str) -> str:
    """Categorize a window into an activity type."""
    title_lower = title.lower() if title else ""
    app_lower = app_id.lower() if app_id else ""

    # Chinese learning
    if any(kw in title_lower for kw in [
        "chinese", "chin", "pinyin", "hanzi", "lesson", "dictation",
        "cumulative", "character-sheet", "textbook", "dialogue",
        "listening", "radical", "tone", "quiz",
    ]):
        return "chinese_learning"

    if "firefox" in app_lower or "chromium" in app_lower:
        if any(kw in title_lower for kw in ["github", "stackoverflow", "docs"]):
            return "coding_reference"
        if any(kw in title_lower for kw in ["youtube", "reddit", "twitter", "news"]):
            return "browsing_media"
        return "browsing_other"

    if app_lower in ("kitty", "foot", "footclient", "alacritty", "wezterm"):
        if any(kw in title_lower for kw in ["code", "codex", "jcode", "claude", "nvim", "vim", "helix"]):
            return "coding"
        return "terminal"

    if "code" in app_lower or "cursor" in app_lower:
        return "coding"

    if any(kw in app_lower for kw in ["evince", "zathura", "okular", "pdf"]):
        return "reading"

    if "muse" in title_lower or "eeg" in title_lower:
        return "eeg_monitor"

    return "other"


def run_tracker(session_dir: str):
    """Subscribe to niri event stream and log focus changes."""
    focus_path = os.path.join(session_dir, "focus.csv")
    is_new = not os.path.exists(focus_path) or os.path.getsize(focus_path) == 0

    focus_file = open(focus_path, "a", newline="", buffering=1)
    writer = csv.writer(focus_file)
    if is_new:
        writer.writerow([
            "timestamp", "window_id", "app_id", "title",
            "workspace_id", "category",
        ])
        focus_file.flush()

    # Get initial focused window
    try:
        result = subprocess.run(
            ["niri", "msg", "-j", "focused-window"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            w = json.loads(result.stdout)
            cat = categorize_window(w.get("title", ""), w.get("app_id", ""))
            writer.writerow([
                f"{time.time():.6f}", w.get("id"), w.get("app_id"),
                w.get("title", ""), w.get("workspace_id"), cat,
            ])
            print(f"  Initial: [{cat}] {w.get('app_id')} — {w.get('title', '')}")
    except Exception as e:
        print(f"  Could not get initial window: {e}")

    # Subscribe to event stream
    print(f"Logging focus changes to: {focus_path}")
    print("Ctrl-C to stop\n")

    proc = subprocess.Popen(
        ["niri", "msg", "-j", "event-stream"],
        stdout=subprocess.PIPE, text=True,
    )

    # Track current windows for lookup
    windows = {}
    last_focused_id = None
    event_count = 0

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Update window list
            if "WindowsChanged" in event:
                for w in event["WindowsChanged"].get("windows", []):
                    windows[w["id"]] = w

            # Focus change
            if "WindowFocusChanged" in event:
                wid = event["WindowFocusChanged"].get("id")
                if wid is not None and wid != last_focused_id:
                    last_focused_id = wid
                    w = windows.get(wid, {})
                    title = w.get("title", "unknown")
                    app_id = w.get("app_id", "unknown")
                    workspace = w.get("workspace_id", "?")
                    cat = categorize_window(title, app_id)

                    ts = time.time()
                    writer.writerow([
                        f"{ts:.6f}", wid, app_id, title, workspace, cat,
                    ])
                    event_count += 1
                    print(f"  [{cat:>20}] {app_id} — {title}")

            # Window title changed (e.g. switching Firefox tabs)
            if "WindowOpenedOrChanged" in event:
                w = event["WindowOpenedOrChanged"].get("window", {})
                if w:
                    windows[w["id"]] = w
                    # If this is the focused window and title changed, log it
                    if w["id"] == last_focused_id and w.get("is_focused"):
                        title = w.get("title", "unknown")
                        app_id = w.get("app_id", "unknown")
                        workspace = w.get("workspace_id", "?")
                        cat = categorize_window(title, app_id)

                        ts = time.time()
                        writer.writerow([
                            f"{ts:.6f}", w["id"], app_id, title, workspace, cat,
                        ])
                        event_count += 1
                        print(f"  [{cat:>20}] {app_id} — {title} (title change)")

    except KeyboardInterrupt:
        print(f"\nStopped. Logged {event_count} focus changes.")
    finally:
        proc.terminate()
        focus_file.close()


def main():
    p = argparse.ArgumentParser(description="Track window focus for EEG correlation")
    p.add_argument("--session", help="Session directory to log into")
    p.add_argument("--standalone", action="store_true", help="Create standalone log file")
    args = p.parse_args()

    if args.session:
        session_dir = args.session
    elif args.standalone:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = f"muse_data/session_{stamp}"
    else:
        session_dir = get_latest_session()
        if not session_dir:
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            session_dir = f"muse_data/session_{stamp}"

    os.makedirs(session_dir, exist_ok=True)
    print(f"Session: {session_dir}")
    run_tracker(session_dir)


if __name__ == "__main__":
    main()
