# Optional tools

The MCP server only reads the local database. This folder contains a separate,
opt-in automation script with substantially different side effects. The summary
skill must not run it automatically to fill a gap or fix an incomplete result.

## `scroll_backfill.py`: real input, unverified chat target

This Windows script brings LINE to the foreground, sends a synthetic Alt key,
moves your **real mouse cursor**, and sends real wheel input at a calculated
point in the window. LINE may then fetch older messages over the network and
persist them in its local encrypted database. Opening or focusing a conversation
may also affect read status. None of these actions is part of a passive MCP read.

**The script cannot verify which conversation is visible.** The `chat_name`
argument resolves a name in local group/OpenChat tables only to choose database
statistics to monitor. It does not select the chat or compare its ID with the
visible pane. Duplicate names may resolve to the first local match. Focus, layout,
window changes, or a mistaken manual selection can send input to the wrong place
while the script monitors another chat.

The script reads the database directly. The MCP server's chat allowlist, contact
gate, pagination, and cumulative output budgets do not apply to it. Do not treat
permission to summarize a chat as permission to run this script.

## Before choosing to run it

1. Read the code and decide whether these input, network, local-storage, and
   possible read-status effects are acceptable. Use only an account and data you
   are authorized to access.
2. Keep LINE running and logged in. Manually open and inspect the intended
   conversation, including its identity if names are duplicated. This manual
   check does not give the script a technical target guarantee.
3. Use an ordinary user terminal. Do not elevate privileges or disable security
   controls to force memory access.
4. Keep work saved and avoid other mouse or keyboard use while the script runs.
   It captures window geometry once; resizing or moving the window can invalidate
   the input target. Stop the process if it targets the wrong pane; it has no
   built-in target-verification or recovery guarantee.

## Usage

Install the optional dependency into the same reviewed Python environment:

```powershell
.\.venv\Scripts\python.exe -m pip install uiautomation
.\.venv\Scripts\python.exe tools/scroll_backfill.py "<exact chat name>" --max-rounds 60 --stall-limit 3 --ticks-per-round 15
```

`uiautomation` is not needed for the MCP server and is intentionally separate
from its runtime requirements. The command above uses the script's defaults.
Supply only positive, bounded round/tick values; this script is not a hardened
server API.

The script stops at `--max-rounds`, or when the earliest locally stored timestamp
does not move earlier for `--stall-limit` consecutive rounds. Neither stopping
condition proves that all history has been fetched, that the visible chat was
correct, or that synchronization is complete. Network delays and LINE behavior
can change the result.

Each process extracts the key afresh; the original implementation reported about
80 seconds, but timing varies. The application does not intentionally persist
the key. OS paging, hibernation, crash dumps, and other memory capture can still
retain it. There is no secure-erasure guarantee.

The original project reported successful scrolling on its tested setup. This
safety repair did not run the script or perform live Windows/LINE validation.
