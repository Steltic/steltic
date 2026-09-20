"""A tool call the model never finished must not poison the conversation.

2026-09-20, a batch design on OpenRouter/Together: the model's turn was cut off right after the
opening brace of a tool call's arguments (`{`). The agent appended the turn as it came, ran the
tool with empty arguments, and the next request was refused by the provider --
`400 Invalid JSON in tool call arguments: '{'` -- a 400 the retry net rightly does not retry. The
run died, and because the turn was already in conversation.json, so did every Continue after it.

Now every tool_call that enters the history parses as a JSON object (a malformed one becomes `{}`),
the call is answered with an error instead of being run, and a conversation saved by an older
version is healed when it is resumed. No model, no server, no sandbox: the chat is scripted.
"""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from steltic import agent                      # noqa: E402
from steltic.job_tools import JobWorkspace     # noqa: E402


def test_heal_tool_calls_rewrites_only_what_would_not_parse():
    calls = [
        {"id": "a", "type": "function", "function": {"name": "write_file", "arguments": "{"}},
        {"id": "b", "type": "function", "function": {"name": "list_files", "arguments": ""}},
        {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "cfg.py"}'}},
        {"id": "d", "type": "function", "function": {"name": "read_file", "arguments": {"path": "x"}}},
        {"id": "e", "type": "function", "function": {"name": "read_file", "arguments": "[1, 2]"}},
    ]
    bad = agent._heal_tool_calls(calls)
    assert set(bad) == {"a", "e"} and bad["a"] == "{"
    for tc in calls:
        assert isinstance(json.loads(tc["function"]["arguments"]), dict)
    assert calls[1]["function"]["arguments"] == "{}"                 # no arguments is a legitimate call, not malformed
    assert calls[2]["function"]["arguments"] == '{"path": "cfg.py"}'  # untouched
    assert json.loads(calls[3]["function"]["arguments"]) == {"path": "x"}


class _Sandbox:
    name = "none"


def _scripted(turns, seen):
    """agent._chat_with_retry stand-in: records the messages of every call, plays `turns` in order."""
    def fake(base_url, api_key, model, messages, max_tok, reasoning=None, cache=False, provider="", cancel=None):
        seen.append(copy.deepcopy(messages))
        t = turns[len(seen) - 1]
        if isinstance(t, BaseException):
            raise t
        if False:
            yield None
        return dict({"content": "", "tool_calls": [], "finish_reason": "stop", "usage": None}, **t)
    return fake


def test_a_cut_off_tool_call_is_not_run_and_the_history_stays_valid(tmp_path, monkeypatch):
    seen, ran = [], []
    turns = [
        {"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{"}}], "finish_reason": "length"},
        {"tool_calls": [{"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "cfg.py", "content": "x = 1"}'}}]},
        agent.UserStop(),
    ]
    monkeypatch.setattr(agent, "_chat_with_retry", _scripted(turns, seen))
    monkeypatch.setattr(agent, "dispatch", lambda nm, args, ws, ex: (ran.append((nm, args)), {"ok": True, "path": args.get("path")})[1])
    ws = JobWorkspace(tmp_path / "jobs")
    events = list(agent.run_design(ws, _Sandbox(), "http://llm.invalid/v1", "k", "fake-model", "B1", "a two-storey office"))
    # the cut-off call: never dispatched, answered with an error the model can act on
    assert ran == [("write_file", {"path": "cfg.py", "content": "x = 1"})]
    second = seen[1]
    tool_turn = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")][0]
    assert tool_turn["tool_calls"][0]["function"]["arguments"] == "{}"
    answer = [m for m in second if m.get("role") == "tool" and m.get("tool_call_id") == "c1"][0]
    assert "not valid JSON" in answer["content"] and "cut off" in answer["content"] and "NOT run" in answer["content"]
    assert any(e.get("type") == "status" and "not valid JSON" in e.get("text", "") for e in events)
    assert any(e.get("type") == "tool_result" and "not valid JSON" in json.dumps(e) for e in events)
    # the saved conversation parses everywhere a provider will look
    conv = json.loads((tmp_path / "jobs" / "B1" / "conversation.json").read_text(encoding="utf-8"))
    for m in conv:
        for tc in m.get("tool_calls") or []:
            assert isinstance(json.loads(tc["function"]["arguments"]), dict)
    assert events[-1]["type"] == "paused" and "stopped" in events[-1]["reason"]


def test_a_conversation_saved_with_a_cut_off_call_is_healed_on_resume(tmp_path, monkeypatch):
    jd = tmp_path / "jobs" / "B2"
    jd.mkdir(parents=True)
    (jd / "conversation.json").write_text(json.dumps([
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "brief"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old", "type": "function", "function": {"name": "write_file", "arguments": "{"}}]},
        {"role": "tool", "tool_call_id": "old", "name": "write_file", "content": "{}"},
    ]), encoding="utf-8")
    seen = []
    monkeypatch.setattr(agent, "_chat_with_retry", _scripted([agent.UserStop()], seen))
    ws = JobWorkspace(tmp_path / "jobs")
    events = list(agent.run_design(ws, _Sandbox(), "http://llm.invalid/v1", "k", "fake-model", "B2", "", resume=True))
    assert any(e.get("type") == "status" and "resumed" in e.get("text", "") for e in events)
    healed = [m for m in seen[0] if m.get("role") == "assistant"][0]
    assert healed["tool_calls"][0]["function"]["arguments"] == "{}"      # what the provider now sees
    conv = json.loads((jd / "conversation.json").read_text(encoding="utf-8"))
    assert conv[2]["tool_calls"][0]["function"]["arguments"] == "{}"      # and what the next Continue loads


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
