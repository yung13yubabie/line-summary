"""Export authorized local LINE history with pseudonymous sender labels.

Uses the project's server.line_get_history: settings.json must explicitly
enable the intended account/chat/date scope. Existing server response and
session budgets apply to every page. This tool never expands them, resets the
server, chooses an account database, or extracts a key itself.

The range is half-open [since, until). Follow opaque server cursors until local
history ends or the explicit --limit is reached (default 5000). Failed or
blocked reads produce no export. Reaching the limit creates an explicitly
partial export, recorded in the accompanying _export_info.json.

Sender names become stable per-export codes (A, B, C, ...). This is NOT full
anonymization: message text, URLs, IDs and other metadata remain unchanged, and
the sender map contains the original names. Keep all generated files private
and out of Git. Within this repository, output must be under ignored output/;
otherwise choose a private directory outside Git repositories.

Example:
    python tools/build_line_community_dataset.py --chat-id CHAT_ID \
        --label example --since 2024-01-01T00:00:00+08:00 \
        --until 2024-01-02T00:00:00+08:00 --out-dir output/private-dataset
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import line_mcp_server as server  # noqa: E402


class ExportError(ValueError):
    """A page or output destination prevents a reliable private export."""


def collect_history(chat_id: str, since: str, until: str, limit: int = 5000,
                    *, history_client=None) -> dict:
    """Collect server pages without bypassing authorization or decoding cursors.

    The actual server validates timezone-aware dates, the half-open range and
    local policy. The injected client exists for synthetic tests, not a CLI
    alternative to those controls. Local completion is never remote-sync proof.
    """
    if type(limit) is not int or limit <= 0:
        raise ExportError("limit must be a positive integer")
    client = history_client if history_client is not None else server.line_get_history
    messages, seen_ids, seen_cursors = [], set(), set()
    cursor = None
    while len(messages) < limit:
        requested = min(500, limit - len(messages))
        result = client(chat_id=chat_id, since=since, until=until,
                        limit=requested, cursor=cursor)
        if (not isinstance(result, dict) or not isinstance(result.get("items"), list)
                or type(result.get("has_more")) is not bool):
            raise ExportError("Invalid history page; export aborted")
        if result.get("content_complete") is not True or result.get("blocked_reason"):
            raise ExportError("Blocked or incomplete history page; export aborted")
        items, more = result["items"], result["has_more"]
        if len(items) > requested:
            raise ExportError("History page exceeded the requested limit; export aborted")
        for item in items:
            mid = item.get("message_id") if isinstance(item, dict) else None
            if not isinstance(mid, str) or not mid or mid in seen_ids:
                raise ExportError("Missing or repeated message ID; export aborted")
            seen_ids.add(mid)
        if more:
            next_cursor = result.get("next_cursor")
            if (not items or not isinstance(next_cursor, str) or not next_cursor
                    or next_cursor in seen_cursors):
                raise ExportError("History pagination cannot advance; export aborted")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        messages.extend(items)
        if not more:
            return {"messages": messages, "local_range_complete": True,
                    "truncated_by_limit": False}
    return {"messages": messages, "local_range_complete": False,
            "truncated_by_limit": True}


def _next_code(index: int) -> str:
    """0->A, 1->B, ..., 25->Z, 26->AA, 27->AB, ..."""
    letters = []
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def pseudonymize(messages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Replace each distinct sender display name with a stable code.
    Returns (messages_with_codes, code -> original_display_name map)."""
    reverse: dict[str, str] = {}
    code_map: dict[str, str] = {}
    out = []
    for msg in messages:
        name = msg.get("sender") or "unknown"
        if name not in reverse:
            code = _next_code(len(reverse))
            reverse[name] = code
            code_map[code] = name
        new_msg = dict(msg)
        new_msg["sender"] = reverse[name]
        out.append(new_msg)
    return out, code_map


def _output_paths(out_dir: str, label: str, since: str, until: str) -> dict:
    if not re.fullmatch(r"[\w-]{1,80}", label):
        raise ExportError("label must contain 1-80 letters, digits, underscores or hyphens")
    destination = Path(out_dir).resolve()
    # Refuse tracked destinations without changing any repository's ignore rules.
    for ancestor in (destination, *destination.parents):
        if (ancestor / ".git").exists():
            if ancestor != ROOT or not destination.is_relative_to(ROOT / "output"):
                raise ExportError("Choose output/ here, or a private directory outside Git repositories")
            break
    stem = f"{label}_{since[:10]}_{until[:10]}"
    paths = {"messages": destination / f"{stem}.json",
             "senders": destination / f"{stem}_sender_map.json",
             "info": destination / f"{stem}_export_info.json"}
    if any(path.exists() for path in paths.values()):
        raise ExportError("Export files already exist; choose a new label or directory")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--label", required=True, help="short community label for output filenames")
    parser.add_argument("--since", required=True, help="ISO 8601 with timezone offset")
    parser.add_argument("--until", required=True, help="ISO 8601 with timezone offset")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args(argv)
    created = []
    try:
        paths = _output_paths(args.out_dir, args.label, args.since, args.until)
        collected = collect_history(args.chat_id, args.since, args.until, args.limit)
        pseudo_messages, code_map = pseudonymize(collected["messages"])
        info = {"since": args.since, "until_exclusive": args.until,
                "coverage": "local_rows_only", "consistency": "live_keyset_scan",
                "database_changes_may_affect_pagination": True,
                "local_range_complete": collected["local_range_complete"],
                "truncated_by_limit": collected["truncated_by_limit"],
                "requested_limit": args.limit, "message_count": len(pseudo_messages),
                "pseudonymization": "sender_labels_only_content_and_metadata_unchanged"}
        paths["messages"].parent.mkdir(parents=True, exist_ok=True)
        for name, payload in (("messages", pseudo_messages), ("senders", code_map), ("info", info)):
            with paths[name].open("x", encoding="utf-8") as handle:
                created.append(paths[name])
                json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as error:
        # Never expose raw backend diagnostics or leave a successful-looking
        # fragment. Remove only files this invocation created, never old exports.
        cleanup_failed = False
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        message = str(error) if isinstance(error, ExportError) else type(error).__name__
        status = ("Partial files could not be removed; check the output directory."
                  if cleanup_failed else "No export files remain.")
        print(f"Export failed: {message}. {status}", file=sys.stderr)
        return 1

    print(f"messages={len(pseudo_messages)} distinct_senders={len(code_map)}")
    print(f"local_range_complete={str(info['local_range_complete']).lower()}")
    print(f"truncated_by_limit={str(info['truncated_by_limit']).lower()}")
    print("Private export: sender labels only; text and other metadata are not anonymized.")
    print(f"written: {paths['messages']}")
    print(f"sender map (private; never commit): {paths['senders']}")
    print(f"coverage information: {paths['info']}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
