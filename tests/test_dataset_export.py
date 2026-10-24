"""Private exports use the authorized paged server, with synthetic data only."""
import json

import pytest

from tools import build_line_community_dataset as export


SINCE = "2024-01-01T00:00:00+08:00"
UNTIL = "2024-01-02T00:00:00+08:00"


def message(mid, sender, content):
    return {"message_id": mid, "type": "text", "sender": sender,
            "content": content, "sent_at": "2024-01-01T10:00:00+08:00"}


def page(items, *, more=False, cursor=None, complete=True, **extra):
    return {"items": items, "has_more": more, "next_cursor": cursor,
            "content_complete": complete, **extra}


def cli_args(out_dir, *, limit=5, label="example"):
    return ["--chat-id", "example-chat", "--label", label,
            "--since", SINCE, "--until", UNTIL,
            "--out-dir", str(out_dir), "--limit", str(limit)]


@pytest.fixture
def private_output(tmp_path, monkeypatch):
    # Each test has an isolated repository/output location even when pytest's
    # basetemp itself is under the real repository's ignored scratchpad/.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(export, "ROOT", tmp_path)
    return tmp_path / "output" / "private-export"


def test_collect_keeps_scope_and_opaque_cursor_until_explicit_limit():
    calls = []
    replies = [
        page([message("one", "測試甲", "第一句中文"),
              message("two", "測試乙", "第二句中文")],
             more=True, cursor="opaque/游標=?"),
        page([message("three", "測試甲", "第三句中文")],
             more=True, cursor="another-opaque-token"),
    ]

    def client(**kwargs):
        calls.append(kwargs)
        return replies[len(calls) - 1]

    result = export.collect_history("example-chat", SINCE, UNTIL, limit=3,
                                    history_client=client)
    assert [m["content"] for m in result["messages"]] == [
        "第一句中文", "第二句中文", "第三句中文",
    ]
    assert calls == [
        {"chat_id": "example-chat", "since": SINCE, "until": UNTIL,
         "limit": 3, "cursor": None},
        {"chat_id": "example-chat", "since": SINCE, "until": UNTIL,
         "limit": 1, "cursor": "opaque/游標=?"},
    ]
    assert result["local_range_complete"] is False
    assert result["truncated_by_limit"] is True


@pytest.mark.parametrize("blocked", [
    page([], more=True, complete=False, blocked_reason="item_exceeds_byte_budget"),
    page([], more=True, cursor="stalled-cursor"),
    page([message("two", "測試乙", "不可當完整成功")], more=True),
])
def test_blocked_or_stalled_second_page_is_a_failure(blocked):
    replies = iter([
        page([message("one", "測試甲", "已讀入但尚未寫檔")],
             more=True, cursor="first-cursor"),
        blocked,
    ])
    with pytest.raises(export.ExportError):
        export.collect_history("example-chat", SINCE, UNTIL, limit=5,
                               history_client=lambda **_: next(replies))


def test_budget_failure_writes_no_partial_success(private_output, monkeypatch, capsys):
    calls = 0

    def client(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return page([message("one", "測試甲", "不能偷偷輸出半份資料")],
                        more=True, cursor="first-cursor")
        raise PermissionError("PRIVATE_BACKEND_DIAGNOSTIC")

    monkeypatch.setattr(export.server, "line_get_history", client)
    out_dir = private_output
    assert export.main(cli_args(out_dir)) == 1
    assert calls == 2
    assert not out_dir.exists()
    diagnostics = capsys.readouterr()
    assert "failed" in diagnostics.err.lower()
    assert "PRIVATE_BACKEND_DIAGNOSTIC" not in diagnostics.err


def test_cli_writes_exact_text_and_explicit_partial_coverage(private_output, monkeypatch, capsys):
    source = [message("one", "測試甲", "保留測試甲這段原文，不是完整去識別化"),
              message("two", "測試乙", "第二段內容")]
    source[1].update(thread_id="example-thread", thread_root_message_id="one")
    monkeypatch.setattr(export.server, "line_get_history", lambda **_: page(
        source, more=True, cursor="opaque-next-page"))
    out_dir = private_output
    assert export.main(cli_args(out_dir, limit=2)) == 0
    stem = "example_2024-01-01_2024-01-02"
    messages = json.loads((out_dir / f"{stem}.json").read_text(encoding="utf-8"))
    sender_map = json.loads((out_dir / f"{stem}_sender_map.json").read_text(encoding="utf-8"))
    info = json.loads((out_dir / f"{stem}_export_info.json").read_text(encoding="utf-8"))
    assert messages == [dict(source[0], sender="A"), dict(source[1], sender="B")]
    assert sender_map == {"A": "測試甲", "B": "測試乙"}
    assert info["local_range_complete"] is False
    assert info["truncated_by_limit"] is True
    assert info["coverage"] == "local_rows_only"
    assert info["since"] == SINCE and info["until_exclusive"] == UNTIL
    assert "cursor" not in json.dumps(info)
    assert "local_range_complete=false" in capsys.readouterr().out


def test_final_page_marks_only_local_range_complete():
    result = export.collect_history("example-chat", SINCE, UNTIL, limit=5,
        history_client=lambda **_: page([message("one", "測試甲", "唯一的中文訊息")]))
    assert result["messages"][0]["content"] == "唯一的中文訊息"
    assert result["local_range_complete"] is True
    assert result["truncated_by_limit"] is False


def test_collect_does_not_send_the_export_limit_as_one_server_page():
    calls = []

    def client(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return page([message(str(i), "測試甲", f"中文第{i}句") for i in range(500)],
                        more=True, cursor="opaque-second-page")
        return page([message("500", "測試甲", "中文第500句")])

    result = export.collect_history("example-chat", SINCE, UNTIL, limit=501,
                                    history_client=client)
    assert [call["limit"] for call in calls] == [500, 1]
    assert len(result["messages"]) == 501
    assert result["messages"][-1]["content"] == "中文第500句"
    assert result["local_range_complete"] is True


def test_bad_filename_label_is_rejected_before_any_read(private_output, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Invalid output filename must be rejected before reading LINE")

    monkeypatch.setattr(export.server, "line_get_history", forbidden)
    assert export.main(cli_args(private_output, label="../escape")) == 1
    assert not private_output.exists()


def test_nonignored_repo_destination_is_rejected_before_any_read(private_output, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("A potentially tracked output destination must not read LINE")

    monkeypatch.setattr(export.server, "line_get_history", forbidden)
    out_dir = export.ROOT / "tracked-data"
    assert export.main(cli_args(out_dir)) == 1
    assert not out_dir.exists()
