"""
Optional real-mouse automation tool -- NOT part of the passive MCP server.
See README.md in this folder before running.

Automates scrolling up inside a specific LINE OpenChat/group window to trigger
LINE's own "load older history" mechanic (verified: manually scrolling makes
LINE fetch older messages from its servers and persist them into the local
encrypted .edb, which line-summary can then read passively).

THIS MOVES YOUR REAL MOUSE CURSOR AND SENDS REAL WHEEL INPUT.
Do not touch the mouse/keyboard while it is running.

Usage:
    python tools/scroll_backfill.py "<exact chat name>" [--max-rounds N] [--stall-limit N]

Stops when N consecutive rounds show no new local message for the chat
(default 3), or when --max-rounds is hit (default 60), whichever first.
"""
import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from line_mcp_server import _find_edb_path  # noqa: E402
from key_extractor import extract_key, find_line_pid  # noqa: E402
from db_reader import _open_encrypted, _ts_to_iso  # noqa: E402

import uiautomation as auto  # noqa: E402

user32 = ctypes.windll.user32
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


def find_line_window():
    win = auto.WindowControl(searchDepth=1, ClassName="AllInOneWindow")
    if not win.Exists(3):
        raise RuntimeError("LINE window (AllInOneWindow) not found via UI Automation.")
    return win


def bring_to_foreground(hwnd):
    # Workaround for Windows' foreground-lock restriction: a synthetic ALT
    # keypress momentarily grants the calling process permission to steal focus.
    user32.keybd_event(0x12, 0, 0, 0)      # ALT down
    user32.keybd_event(0x12, 0, 2, 0)      # ALT up
    user32.SetForegroundWindow(hwnd)


def scroll_up_real(x, y, ticks):
    user32.SetCursorPos(x, y)
    for _ in range(ticks):
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, WHEEL_DELTA, 0)
        time.sleep(0.05)


def chat_stats(conn, chat_id):
    row = list(conn.execute(
        "SELECT count(*) c, min(_createdTime) mn FROM _message WHERE _chatId=?;", (chat_id,)
    ))[0]
    return row["c"], row["mn"]


def resolve_chat_id(conn, chat_name):
    for table, id_col, name_col in [
        ("_squareChat", "_squareChatMid", "_name"),
        ("_groupChat", "_chatMid", "_chatName"),
    ]:
        rows = list(conn.execute(f"SELECT {id_col} id, {name_col} name FROM {table};"))
        for r in rows:
            if r["name"] == chat_name:
                return r["id"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_name")
    ap.add_argument("--max-rounds", type=int, default=60)
    ap.add_argument("--stall-limit", type=int, default=3)
    ap.add_argument("--ticks-per-round", type=int, default=15)
    args = ap.parse_args()

    if find_line_pid() is None:
        print("LINE is not running."); sys.exit(1)

    db = _find_edb_path()
    print("Extracting key (~80s)...")
    key = extract_key(db, require_consent=False)
    if not key:
        print("Key extraction failed."); sys.exit(1)
    conn = _open_encrypted(db, key)
    try:
        chat_id = resolve_chat_id(conn, args.chat_name)
    finally:
        conn.close()
    if not chat_id:
        print(f"Chat {args.chat_name!r} not found in local _squareChat/_groupChat tables.")
        sys.exit(1)

    def current_stats():
        current = _open_encrypted(db, key)
        try:
            return chat_stats(current, chat_id)
        finally:
            current.close()

    win = find_line_window()
    hwnd = win.NativeWindowHandle
    rect = win.BoundingRectangle
    # Proportional target point inside the message pane (right ~50% of window,
    # below the header, above the input box) -- robust to window resize/move.
    target_x = rect.left + int((rect.right - rect.left) * 0.75)
    target_y = rect.top + int((rect.bottom - rect.top) * 0.55)

    print(f"Target chat_id={chat_id}")
    count, earliest = current_stats()
    print(f"START  count={count}  earliest={_ts_to_iso(earliest)}")
    print(f"Scroll target point: ({target_x},{target_y}) inside LINE window {rect}")
    print("\n>>> Moving your real mouse cursor now. Do not touch mouse/keyboard. <<<\n")

    bring_to_foreground(hwnd)
    time.sleep(0.5)

    stall = 0
    round_no = 0
    while round_no < args.max_rounds and stall < args.stall_limit:
        round_no += 1
        scroll_up_real(target_x, target_y, args.ticks_per_round)
        time.sleep(1.2)  # let LINE fetch/render before re-checking
        new_count, new_earliest = current_stats()
        moved = new_earliest is not None and earliest is not None and new_earliest < earliest
        print(f"round {round_no:>3}  count={new_count:>5}  earliest={_ts_to_iso(new_earliest)}"
              f"  {'(older reached)' if moved else ''}")
        if new_earliest == earliest:
            stall += 1
        else:
            stall = 0
        count, earliest = new_count, new_earliest

    print(f"\nSTOPPED after {round_no} rounds "
          f"({'stalled '+str(args.stall_limit)+'x' if stall >= args.stall_limit else 'max-rounds hit'}).")
    print(f"FINAL  count={count}  earliest={_ts_to_iso(earliest)}")
    print("\nDone -- you can use the mouse again.")


if __name__ == "__main__":
    main()
