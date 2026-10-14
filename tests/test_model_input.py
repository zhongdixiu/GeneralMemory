from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from memory_cmic.extraction_input import input_size
from memory_cmic.model_input import (
    compact_json,
    extraction_payload,
    governance_payload,
    request_size,
)
from memory_cmic.providers import (
    EXTRACTION_PROMPT,
    GOVERNANCE_PROMPT,
    ExtractedFact,
    InputLimitError,
    ProviderError,
    QwenFactModel,
)


def _candidates():
    return [{
        "id": f"mem_{i:032x}", "memory": f"用户要求第{i}类邮件使用简洁语句。",
        "business_domains": ["email"], "project_context": None,
        "status": "active", "effective_at": "2026-10-13T08:00:00+00:00",
        "expired_at": "2027-04-13T08:00:00+00:00", "explicit_expired_at": None,
        "conflict_group_id": None, "temporal_kind": "current",
        "source_occurred_at": f"2026-10-13T08:{i:02d}:00+00:00", "timestamp_origin": "source",
        "statement_order": {"caller_agent_id": "agent_" + "a" * 32,
                            "agent_id": "agent_" + "a" * 32, "source_system": "email_agent",
                            "session_id": "session_" + "a" * 64, "batch_seq": i,
                            "phase": 1, "position": 0},
        "source_message_ids": [f"message_{i:032x}"],
    } for i in range(10)]


def _target():
    return {"message_id": "user", "role": "user", "content": "以后邮件请简洁些。",
            "occurred_at": "2026-10-14T09:00:00+08:00"}


def test_ten_candidates_fit_after_compaction_without_losing_decision_fields():
    candidates = _candidates()
    original = deepcopy(candidates)
    fact = ExtractedFact(memory="用户要求邮件简洁。", message_ids=["user"]).model_dump(mode="json")
    payload = governance_payload(fact, candidates, [_target()])
    old = {"fact": fact, "candidates": candidates, "targets": [_target()]}
    assert input_size(GOVERNANCE_PROMPT, old) > 9952
    assert request_size(GOVERNANCE_PROMPT, payload) < 9952
    for index, row in enumerate(payload["candidates"]):
        restored = {**payload.get("candidate_defaults", {}), **row, "id": candidates[index]["id"]}
        if isinstance(restored.get("statement_order"), dict):
            restored["statement_order"] = {
                **payload.get("statement_order_defaults", {}), **restored["statement_order"]}
        assert restored == {key: value for key, value in candidates[index].items()
                            if key not in ("source_message_ids", "expired_at")}
        assert "shared_source" not in restored
    assert "project_limited" not in payload["fact"]
    assert "retention_kind" not in payload["fact"]
    assert candidates == original


def test_mixed_unknown_time_and_dispute_members_survive_compaction():
    candidates = _candidates()[:3]
    candidates[0].update(statement_order=None, source_occurred_at=None,
                         timestamp_origin="adapter_observed", source_message_ids=["user"])
    for candidate in candidates[1:]:
        candidate.update(status="disputed", conflict_group_id="conflict")
    payload = governance_payload({"message_ids": ["user"]}, candidates, [_target()])
    rows = [{**payload.get("candidate_defaults", {}), **row} for row in payload["candidates"]]
    assert len(rows) == 3 and rows[0]["statement_order"] is None
    assert rows[0]["shared_source"] is True
    assert all("source_message_ids" not in row and "expired_at" not in row for row in rows)
    assert all("shared_source" not in row for row in rows[1:])
    assert all(row["status"] == "disputed" and row["conflict_group_id"] == "conflict"
               for row in rows[1:])


@pytest.mark.parametrize("action", ["DUPLICATE", "SUPERSEDE", "DISPUTE", "ADD"])
def test_governance_restores_short_ids_and_keeps_only_cited_users(monkeypatch, action):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    candidates = _candidates()
    def completion(prompt, payload):
        assert [m["message_id"] for m in payload["targets"]] == ["user", "later-user"]
        assert all(set(m).issubset({"message_id", "role", "content", "occurred_at",
                                  "timestamp_origin", "statement_order"})
                   for m in payload["targets"])
        assert [m["id"] for m in payload["candidates"]] == [f"m{i}" for i in range(10)]
        return {"action": action, "memory_ids": [] if action == "ADD" else ["m0"],
                "historical_before_id": "m1" if action == "ADD" else None,
                "justification_message_id": "user", "justification_quote": "以后"}
    monkeypatch.setattr(model, "_json_completion", completion)
    result = model.resolve(
        fact=ExtractedFact(memory="偏好", message_ids=["user", "later-user"]),
        candidates=candidates,
        targets=[_target(), {"role": "assistant", "content": "长回复" * 10000},
                 {**_target(), "message_id": "later-user", "content": "仅限测试邮件。",
                  "received_at": "2026-10-14T09:00:01+08:00", "turn_id": "turn-1"},
                 {**_target(), "message_id": "uncited", "content": "无关正文" * 10000}],
    )
    assert result.memory_ids == ([] if action == "ADD" else [candidates[0]["id"]])
    assert result.historical_before_id == (candidates[1]["id"] if action == "ADD" else None)


def test_governance_rejects_unknown_short_id(monkeypatch):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(model, "_json_completion", lambda *args: {
        "action": "ADD", "memory_ids": [], "historical_before_id": "m99"})
    with pytest.raises(ProviderError, match="unknown memory"):
        model.resolve(fact=ExtractedFact(memory="事实", message_ids=["user"]),
                      candidates=_candidates(), targets=[_target()])


def test_exact_add_payload_size_is_checked_before_sending(monkeypatch):
    target = _target()
    payload = extraction_payload([target], [])
    size = request_size(EXTRACTION_PROMPT, payload)
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1",
                          input_tokens=size + 2048)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs["messages"][1]["content"] == compact_json(payload)
        assert "history" not in payload
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(
                content='{"facts":[],"assessments":[{"message_id":"user",'
                        '"outcome":"no_durable_value","reason":"test"}]}'))])
    monkeypatch.setattr(model._client.chat.completions, "create", create)
    model.extract_with_outcomes(targets=[target], history=[])
    model.input_tokens -= 1
    with pytest.raises(InputLimitError):
        model.extract_with_outcomes(targets=[target], history=[])
    assert len(calls) == 1


def test_governance_checks_the_projected_wire_payload_before_sending(monkeypatch):
    fact = ExtractedFact(memory="用户要求邮件简洁。", message_ids=["user"])
    candidates = _candidates()
    targets = [_target(), {**_target(), "message_id": "uncited", "content": "甲" * 10000}]
    payload = governance_payload(fact.model_dump(mode="json"), candidates, targets)
    size = request_size(GOVERNANCE_PROMPT, payload)
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1",
                          input_tokens=size + 2048)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs["messages"][1]["content"] == compact_json(payload)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"action":"ADD"}'))])
    monkeypatch.setattr(model._client.chat.completions, "create", create)
    assert model.resolve(fact=fact, candidates=candidates, targets=targets).action == "ADD"
    model.input_tokens -= 1
    with pytest.raises(InputLimitError):
        model.resolve(fact=fact, candidates=candidates, targets=targets)
    assert len(calls) == 1


def test_governance_overflow_reports_incomplete_and_continues_other_facts(monkeypatch):
    from memory_cmic import worker
    from memory_cmic.extraction_input import ExtractionBudget

    candidates = _candidates()
    for i, candidate in enumerate(candidates):
        candidate.update(memory="甲" * 1000 + str(i), business_domains=None)
    by_id = {c["id"]: c for c in candidates}
    memories = [SimpleNamespace(id=c["id"], updated_at=i, metadata_json={})
                for i, c in enumerate(candidates)]
    monkeypatch.setattr(worker, "_scope_memories", lambda *args: memories)
    monkeypatch.setattr(worker, "_candidate", lambda m: by_id[m.id])
    monkeypatch.setattr(worker, "_fingerprint", lambda *args: "unchanged")
    monkeypatch.setattr(worker, "_similar_candidates", lambda *args, **kwargs: [])
    targets = [_target(), {**_target(), "message_id": "duplicate"}]
    task_input = SimpleNamespace(targets=targets, related_memories=[])
    facts = [ExtractedFact(memory="用户喜欢简洁回复。", message_ids=["user"]),
             ExtractedFact(memory=candidates[0]["memory"], message_ids=["duplicate"])]
    def no_call():
        raise AssertionError("oversized input must not consume a model call")
    decisions = worker._plan_decisions(
        lambda: nullcontext(None), task_input=task_input, facts=facts,
        embeddings=[[1.0], [1.0]], fact_model=SimpleNamespace(),
        embedder=SimpleNamespace(model_id="fake"), duplicate_threshold=0.85,
        consume_call=no_call, budget=ExtractionBudget(),
    )
    assert decisions[0].skip_reason == "GOVERNANCE_BUDGET_EXCEEDED"
    assert decisions[1].skip_reason is None
    assert decisions[1].governance.memory_ids == [candidates[0]["id"]]
    session = SimpleNamespace(get=lambda *args: SimpleNamespace(metadata_json={}, status="active"))
    results = worker._slice_processing_results(session, task_input, decisions, [], [], set())
    assert results[0]["outcome"] == "context_insufficient"
    assert results[0]["reason_code"] == "GOVERNANCE_BUDGET_EXCEEDED"
    assert results[1]["outcome"] == "evidence_added"


def test_truncated_evidence_is_blocked_even_if_model_claims_it_is_complete(monkeypatch):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    facts = [ExtractedFact(memory="用户喜欢年糕。", message_ids=["user"]),
             ExtractedFact(memory="用户确认了整套方案。", message_ids=["user", "proposal"])]
    monkeypatch.setattr(model, "_json_completion", lambda *args: {
        "facts": [f.model_dump(mode="json") for f in facts],
        "assessments": [{"message_id": "user", "outcome": "evaluated", "reason": "已理解"}],
    })
    output = model.extract_with_outcomes(targets=[_target()], history=[
        {"message_id": "proposal", "role": "assistant", "content": "方案前段",
         "truncated": True, "start_char": 0, "end_char": 4}])
    assert output.facts == [facts[0]]
    assert output.assessments[0].outcome == "context_insufficient"
    assert output.assessments[0].reason_code == "HISTORY_TRUNCATED"


def test_current_fragment_citation_is_not_mistaken_for_truncated_old_history(monkeypatch):
    # Test evidence identity independently of the default input budget.
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1",
                          input_tokens=14000)
    fact = ExtractedFact(memory="用户喜欢年糕。", message_ids=["user"])
    monkeypatch.setattr(model, "_json_completion", lambda *args: {
        "facts": [fact.model_dump(mode="json")],
        "assessments": [{"message_id": "user", "outcome": "evaluated", "reason": "当前片段"}],
    })
    output = model.extract_with_outcomes(
        targets=[{**_target(), "content": "我喜欢年糕。", "start_char": 500, "end_char": 507}],
        history=[{**_target(), "content": "甲" * 300, "truncated": True,
                  "start_char": 0, "end_char": 300, "continuation": True}],
    )
    assert output.facts == [fact]
    assert output.assessments[0].outcome == "evaluated"
