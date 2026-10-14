import pytest

from memory_cmic.extraction_input import (
    ExtractionBudget,
    build_slices,
    input_size,
    select_history,
    split_slice,
)
from memory_cmic.model_input import extraction_payload, extraction_size
from memory_cmic.providers import EXTRACTION_PROMPT


def _message(mid, text, **extra):
    return {
        "message_id": mid,
        "role": "user",
        "content": text,
        "occurred_at": "2026-09-27T09:00:00+08:00",
        **extra,
    }


def test_slices_preserve_every_character_and_budget():
    budget = ExtractionBudget(input_tokens=10000, output_tokens=512)
    text = ("甲乙丙丁。\n" * 500) + "尾部"
    slices = build_slices([_message("long", text)], [], system=EXTRACTION_PROMPT, budget=budget)
    assert len(slices) > 1
    assert "".join(s["targets"][0]["content"] for s in slices) == text
    cursor = 0
    for current in slices:
        fragment = current["targets"][0]
        assert fragment["start_char"] == cursor
        cursor = fragment["end_char"]
        assert text[fragment["start_char"] : cursor] == fragment["content"]
        assert (
            input_size(
                EXTRACTION_PROMPT, {"targets": current["targets"], "history": current["history"]}
            )
            <= 9488
        )
    assert cursor == len(text)


def test_small_messages_share_slice_and_far_history_is_trimmed():
    targets = [_message("m1", "请简洁回复邮件。"), _message("m2", "称呼我林舟。")]
    history = [_message("old", "无关历史" * 10000)]
    slices = build_slices(targets, history, system=EXTRACTION_PROMPT, budget=ExtractionBudget())
    assert len(slices) == 1
    assert len(slices[0]["targets"]) == 2
    assert slices[0]["history"] == []


def test_split_preserves_text_offsets_and_prior_context():
    current = {
        "targets": [{**_message("m1", "甲乙丙丁"), "start_char": 10, "end_char": 14}],
        "history": [],
        "split_depth": 0,
    }
    left, right = split_slice(current)
    assert left["targets"][0]["end_char"] == right["targets"][0]["start_char"] == 12
    assert right["history"] == left["targets"]
    assert left["targets"][0]["content"] + right["targets"][0]["content"] == "甲乙丙丁"


def test_required_reply_outside_recent_window_is_retained():
    history = [_message("proposal", "以后周报采用三段式，可以吗？")]
    history.extend(_message(f"ordinary-{i}", "普通历史") for i in range(40))
    slices = build_slices([_message("confirm", "可以，以后都这样。",
                                   reply_to_message_id="proposal")], history,
                          system=EXTRACTION_PROMPT, budget=ExtractionBudget())
    assert any(m["message_id"] == "proposal" for m in slices[0]["history"])


def test_long_linked_history_is_shortened_without_blocking_target():
    history = [_message("huge", "必要依据" * 10000)]
    slices = build_slices([_message("confirm", "可以。", reply_to_message_id="huge")],
                          history, system=EXTRACTION_PROMPT,
                          budget=ExtractionBudget(input_tokens=14000))
    assert not slices[0].get("context_error")
    assert slices[0]["targets"][0]["content"] == "可以。"
    prefix = slices[0]["history"][0]
    assert prefix["truncated"] and prefix["content"] == history[0]["content"][:300]
    assert (prefix["start_char"], prefix["end_char"]) == (0, 300)
    assert len(history[0]["content"]) == 40000 and "truncated" not in history[0]


def test_empty_structured_message_is_preserved_in_slices():
    calls = [{"tool_call_id": "call", "tool_name": "lookup", "arguments": {}}]
    slices = build_slices([_message("call-message", "", role="assistant", tool_calls=calls)], [],
                          system=EXTRACTION_PROMPT, budget=ExtractionBudget())
    assert slices[0]["targets"][0]["tool_calls"] == calls
    assert slices[0]["targets"][0]["start_char"] == slices[0]["targets"][0]["end_char"] == 0


def test_empty_tool_call_is_available_to_later_slice():
    call = _message("call", "", role="assistant", turn_id="turn", tool_calls=[
        {"tool_call_id": "id", "tool_name": "lookup", "arguments": {"q": "x" * 80}},
    ])
    target = _message("confirm", "继续采用这个安排。", turn_id="turn")
    limit = max(input_size("", {"targets": [{**m, "content": m["content"][:1],
                                             "start_char": 0,
                                             "end_char": min(1, len(m["content"]))}],
                               "history": []}) for m in [call, target]) + 20
    slices = build_slices([call, target], [], system="",
                          budget=ExtractionBudget(input_tokens=limit + 128, output_tokens=128))
    assert len(slices) == 2
    assert not slices[1].get("context_error")
    assert slices[1]["targets"][0]["content"] == target["content"]
    assert extraction_size("", slices[1]["targets"], slices[1]["history"]) <= limit


def test_optional_state_hints_never_force_short_target_into_fragments():
    target = _message("new", "独立完整的短消息")
    history = [_message("old", "旧计划")]
    related = [{"id": "memory", "message_ids": ["old"], "memory": "甲" * 5000}]
    slices = build_slices([target], history, system=EXTRACTION_PROMPT,
                          budget=ExtractionBudget(), related_memories=related)
    assert len(slices) == 1 and slices[0]["targets"][0]["content"] == target["content"]
    assert not slices[0].get("related_memories")


def test_state_hint_support_can_be_kept_beyond_ordinary_window():
    history = [_message("old", "用户准备去动物园。")]
    history.extend(_message(str(i), "普通历史") for i in range(35))
    related = [{"id": "memory", "message_ids": ["old"], "memory": "用户准备去动物园。"}]
    slices = build_slices([_message("new", "确切说周六。")], history,
                          system=EXTRACTION_PROMPT, budget=ExtractionBudget(),
                          related_memories=related)
    assert slices[0]["related_memories"] == related
    assert any(m["message_id"] == "old" for m in slices[0]["history"])
    payload = {key: slices[0][key] for key in ("targets", "history", "related_memories")}
    assert input_size(EXTRACTION_PROMPT, payload) <= 9952


@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
def test_every_historical_role_uses_same_prefix_rule(role):
    target = _message("new", "我喜欢米粉。")
    old = _message("old", "甲" * 10000, role=role)
    limit = extraction_size("", [target], []) + 1600
    selected = select_history([target], [old], system="", limit=limit)
    assert len(selected) == 1 and selected[0]["content"] == "甲" * 300
    assert selected[0]["truncated"]
    assert extraction_size("", [target], selected) <= limit


def test_empty_history_is_omitted_but_empty_tool_calls_are_kept():
    target = _message("new", "事实")
    empty = _message("empty", "  ")
    assert "history" not in extraction_payload([target], [])
    assert select_history([target], [empty], system="", limit=10000) == []
    tool = _message("call", "", role="assistant", tool_calls=[{
        "tool_call_id": "id", "tool_name": "lookup", "arguments": {}}])
    assert select_history([target], [tool], system="", limit=10000) == [tool]


def test_long_agent_and_tool_targets_preserve_all_text_across_slices():
    targets = [_message("agent", "方案说明。" * 1800, role="assistant"),
               _message("tool", "工具返回。" * 1800, role="tool"),
               _message("user", "只用于测试环境。")]
    slices = build_slices(targets, [], system=EXTRACTION_PROMPT, budget=ExtractionBudget())
    for target in targets:
        pieces = [m for current in slices for m in current["targets"]
                  if m["message_id"] == target["message_id"]]
        assert "".join(m["content"] for m in pieces) == target["content"]
        assert pieces[0]["start_char"] == 0
        assert pieces[-1]["end_char"] == len(target["content"])
        assert all(a["end_char"] == b["start_char"] for a, b in zip(pieces, pieces[1:]))
    for current in slices:
        if current.get("context_only"):
            assert all(m["role"] != "user" for m in current["targets"])
            continue
        assert extraction_size(EXTRACTION_PROMPT, current["targets"], current["history"],
                               current.get("related_memories")) <= 9952


def test_history_incremental_size_agrees_with_final_request():
    target = _message("target", '中文\\n"😀')
    history = [_message(str(i), '内容\\"😀' * (i + 1)) for i in range(40)]
    base = extraction_size("prompt", [target], [])
    for allowance in [0, 1, 200, 700, 3000, 10000]:
        selected = select_history([target], history, system="prompt", limit=base + allowance)
        assert selected is not None
        assert extraction_size("prompt", [target], selected) <= base + allowance
