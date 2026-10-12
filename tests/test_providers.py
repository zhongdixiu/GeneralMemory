from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memory_cmic.providers import ExtractedFact, OutputLimitError, ProviderError, QwenFactModel


def _model(monkeypatch, facts, *, complete=True):
    if complete:
        facts = [
            {
                **ExtractedFact(memory=f["memory"], message_ids=f["message_ids"]).model_dump(
                    mode="json"
                ),
                **f,
            }
            for f in facts
        ]
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(model, "_json_completion", lambda *args: {"facts": facts})
    return model


@pytest.mark.parametrize("role", ["assistant", "system", "tool"])
def test_qwen_rejects_evidence_without_target_user(monkeypatch, role):
    model = _model(monkeypatch, [{"memory": "用户喜欢简洁回复。", "message_ids": ["non-user"]}])
    with pytest.raises(ProviderError, match="target user"):
        model.extract(targets=[{"message_id": "non-user", "role": role}], history=[])


def test_qwen_accepts_user_confirmation_with_assistant_context(monkeypatch):
    model = _model(
        monkeypatch, [{"memory": "用户喜欢简洁回复。", "message_ids": ["assistant", "user"]}]
    )
    assert model.extract(
        targets=[
            {"message_id": "assistant", "role": "assistant", "content": "以后简洁回复？"},
            {"message_id": "user", "role": "user", "content": "是的。"},
        ],
        history=[],
    ) == [ExtractedFact(memory="用户喜欢简洁回复。", message_ids=["assistant", "user"])]


def test_qwen_rejects_history_without_target_user_anchor(monkeypatch):
    model = _model(monkeypatch, [{"memory": "用户喜欢简洁回复。", "message_ids": ["history"]}])
    with pytest.raises(ProviderError, match="target user"):
        model.extract(
            targets=[{"message_id": "target", "role": "user"}],
            history=[{"message_id": "history", "role": "user"}],
        )


def test_qwen_requires_explicit_condition_and_temporal_fields(monkeypatch):
    model = _model(
        monkeypatch, [{"memory": "用户喜欢简洁回复。", "message_ids": ["user"]}], complete=False
    )
    with pytest.raises(ProviderError, match="missing applicability"):
        model.extract(targets=[{"message_id": "user", "role": "user"}], history=[])


def test_qwen_accepts_personal_preference_without_scope_known(monkeypatch):
    fact = {
        "memory": "用户喜欢吃年糕。",
        "message_ids": ["user"],
        "business_domains": None,
        "project_limited": False,
        "project_context": None,
        "temporal_kind": "current",
        "retention_kind": "long_term",
        "change_kind": "none",
        "effective_at": None,
        "expired_at": None,
    }
    model = _model(monkeypatch, [fact], complete=False)
    facts = model.extract(
        targets=[{"message_id": "user", "role": "user", "content": "我喜欢吃年糕。"}],
        history=[],
    )
    assert len(facts) == 1
    assert facts[0].memory == "用户喜欢吃年糕。"
    assert facts[0].business_domains is None
    assert facts[0].retention_kind == "long_term"


def test_qwen_requires_explicit_retention_kind(monkeypatch):
    fact = ExtractedFact(memory="用户喜欢简洁回复。", message_ids=["user"]).model_dump(mode="json")
    fact.pop("retention_kind")
    model = _model(monkeypatch, [fact], complete=False)
    with pytest.raises(ProviderError, match="missing applicability"):
        model.extract(targets=[{"message_id": "user", "role": "user"}], history=[])


def test_invalid_retention_kind_is_rejected():
    with pytest.raises(ValidationError):
        ExtractedFact(memory="事实", message_ids=["user"], retention_kind="permanent")


@pytest.mark.parametrize("scope", [[], [" "], ["x" * 33]])
def test_invalid_business_domain_is_rejected(scope):
    with pytest.raises(ValidationError):
        ExtractedFact(memory="偏好", message_ids=["user"], business_domains=scope)


def test_business_domains_normalize_and_source_does_not_set_scope():
    assert ExtractedFact(
        memory="偏好", message_ids=["user"], business_domains=["email", "disk", "email"]
    ).business_domains == ["disk", "email"]
    assert ExtractedFact(memory="称呼", message_ids=["user"]).business_domains is None


@pytest.mark.parametrize(
    "values",
    [
        {"effective_at": "2026-09-27T00:00:00"},
        {"effective_at": "2026-09-27T00:00:00Z", "expired_at": "2026-09-26T00:00:00Z"},
    ],
)
def test_fact_time_requires_timezone_and_valid_interval(values):
    with pytest.raises(ValidationError):
        ExtractedFact(memory="历史事实", message_ids=["user"], **values)


def test_truncated_completion_is_not_accepted_as_partial_json(monkeypatch):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="length", message=SimpleNamespace(content='{"facts": []}')
            )
        ]
    )
    monkeypatch.setattr(model._client.chat.completions, "create", lambda **kwargs: response)
    with pytest.raises(OutputLimitError):
        model._json_completion("prompt", {})


def test_governance_cannot_reference_unknown_id(monkeypatch):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(
        model,
        "_json_completion",
        lambda *args: {
            "action": "DUPLICATE",
            "memory_ids": ["unknown"],
        },
    )
    with pytest.raises(ProviderError, match="unknown memory"):
        model.resolve(
            fact=ExtractedFact(memory="事实", message_ids=["user"]), candidates=[], targets=[]
        )


def test_open_business_labels_and_initial_aliases():
    fact = ExtractedFact(
        memory="偏好",
        message_ids=["user"],
        business_domains=["方案编写", "邮件", "email", "cloud_drive"],
    )
    assert fact.business_domains == ["disk", "email", "方案编写"]


def test_project_context_is_required_without_assigning_project_id():
    with pytest.raises(ValidationError, match="project_context"):
        ExtractedFact(memory="项目要求", message_ids=["user"], project_limited=True)
    fact = ExtractedFact(
        memory="星河项目总结用列表",
        message_ids=["user"],
        project_limited=True,
        project_context="星河项目",
    )
    from memory_cmic.providers import candidate_skip_reason

    assert candidate_skip_reason(fact) is None


def test_applicability_rejects_unknown_ids(monkeypatch):
    model = QwenFactModel(api_key="fake", base_url="https://example.invalid/v1")
    monkeypatch.setattr(model, "_json_completion", lambda *args: {"memory_ids": ["unknown"]})
    with pytest.raises(ProviderError, match="unknown memory"):
        model.select_applicable(query="项目总结", business_domain="email", candidates=[])


def test_qwen_accepts_history_evidence_with_target_user_anchor(monkeypatch):
    model = _model(monkeypatch, [{"memory": "用户要求周报采用三段式。",
                                 "message_ids": ["proposal", "confirm"]}])
    facts = model.extract(targets=[{"message_id": "confirm", "role": "user"}],
                          history=[{"message_id": "proposal", "role": "assistant"}])
    assert facts[0].message_ids == ["proposal", "confirm"]


def test_qwen_rejects_missing_and_inconsistent_assessments(monkeypatch):
    model = _model(monkeypatch, [{"memory": "用户喜欢简洁回复。", "message_ids": ["user"]}])
    fact = ExtractedFact(memory="用户喜欢简洁回复。", message_ids=["user"]).model_dump(mode="json")
    for assessments in ([], [{"message_id": "user", "outcome": "no_durable_value",
                             "reason": "没有保存价值"}]):
        monkeypatch.setattr(model, "_json_completion", lambda *args, a=assessments: {
            "facts": [fact], "assessments": a})
        with pytest.raises(ProviderError):
            model.extract_with_outcomes(targets=[{"message_id": "user", "role": "user"}],
                                        history=[])


def test_observation_timestamp_is_hidden_from_extraction_model(monkeypatch):
    model = _model(monkeypatch, [])

    def completion(prompt, payload):
        assert payload["targets"][0]["occurred_at"] is None
        assert payload["targets"][0]["timestamp_origin"] == "adapter_observed"
        return {"facts": [], "assessments": [{"message_id": "observed",
                "outcome": "context_insufficient", "reason": "原始时间未知"}]}

    monkeypatch.setattr(model, "_json_completion", completion)
    target = {"message_id": "observed", "role": "user", "content": "本周负责审核。",
              "occurred_at": "2026-10-12T09:00:00+08:00", "timestamp_origin": "adapter_observed"}
    model.extract_with_outcomes(targets=[target], history=[])
    assert target["occurred_at"] == "2026-10-12T09:00:00+08:00"
