from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from memory_cmic.ttl import RetentionKind


class ProviderError(RuntimeError):
    pass


class OutputLimitError(ProviderError):
    pass


PROMPT_VERSION = "p02-v11-context-evidence"

EXTRACTION_PROMPT = (
    "Extract atomic facts with value beyond a single task, newly stated or explicitly "
    "confirmed by target user messages. "
    "Write each memory as one short, standalone declarative sentence with an explicit "
    "subject, predicate and object or complement. For facts about the speaker, use 用户 "
    "as the Chinese subject (the user in English), not 我, 我们, 你, I, we or you. "
    "Resolve pronouns, demonstratives and omitted subjects using target messages and history. "
    "History may identify a referent without independently supplying a new fact. "
    "For example, '我喜欢吃年糕。' becomes '用户喜欢吃年糕。'; with history '我最近搬到了杭州。', "
    "'我住在那边。' becomes '用户住在杭州。'. Without a supported place referent, omit that fact. "
    "For '项目方案详细说明。', write '用户要求项目方案提供详细说明。'. "
    "Resolve references to other people to their supported names or roles, not to 用户: "
    "if history identifies 他 as 李明, '以后邮件的审批都交给他负责。' can become "
    "'李明负责以后的邮件审批。'. Never guess a person, place or object. "
    "Keep literal names, titles and verbatim quotations unchanged: '我喜欢电影《我和我的祖国》。' "
    "becomes '用户喜欢电影《我和我的祖国》。', not a replacement inside the title. "
    "History cannot independently produce facts. Cite all necessary target AND history "
    "message_ids as one support group; at least one cited message must be a TARGET user. "
    "A confirmation must cite the proposal/question that gives it meaning. "
    "Attachment references are not readable attachment contents. Never infer user facts from "
    "assistant/system/tool alone. "
    "Business labels describe applicable scenarios, not source systems or mentioned topics. "
    "Use communication for SMS, phone calls and instant messaging (excluding email); "
    "email for reading, writing, replying, processing and organizing email; "
    "disk for personal cloud drive backup, storage, organization and sharing, not local files. "
    "These are initial canonical labels, NOT an exhaustive enum. For other explicit scenarios "
    "extract a short, stable semantic label in the user's language, e.g. 方案编写. "
    "Reuse canonical labels for their synonyms. Split independent applicability requirements. "
    "Personal identity, birthdays, residence, occupations, preferences and habits do not "
    "require a business scenario: use business_domains=null unless the fact itself limits "
    "business applicability. For example, '用户喜欢吃年糕。' is a long_term personal preference "
    "with business_domains=null. Preserve every explicit business, project, customer, "
    "recipient, occasion and temporal condition in memory text. Do not invent restrictions "
    "from the source system or topic. Difficulty assigning a business label must not discard "
    "a complete, supported fact; preserve its stated activity and use a short semantic label "
    "when it is business-limited. A generic project-proposal preference is a proposal-writing "
    "scenario, not a specific project. "
    "Project-related durable memories MUST be preserved. Project IDs are reserved and not "
    "assigned. If limited to a particular project set project_limited=true and project_context "
    "to its explicit name or self-contained identifying context; preserve that condition in "
    "memory text. Otherwise project_limited=false and project_context=null. "
    "Unknown or ambiguous project names do not by themselves invalidate a self-contained "
    "fact. Preserve the supplied project wording without guessing identity. Do not broaden a "
    "project rule into a general preference. A project material directory does not imply disk. "
    "Literal names/placeholders such as 未知项目 and 重名项目 are still project_context; "
    "do not drop them solely because they are unknown/ambiguous. "
    "For '重名项目的材料放在批准目录', preserve that project condition and use an applicable "
    "material-management scenario label (not disk unless cloud drive is stated). "
    "Short standalone requirements such as '项目方案详细说明。' are durable preferences: "
    "extract with a proposal-writing label, project_limited=false. "
    "Use the original language in memory text. "
    "Set retention_kind=temporary for explicitly temporary or stage-specific facts, such as "
    "a current-phase responsibility or a near-term recurring requirement; regular for other "
    "facts; long_term for personal identity, occupations, lasting preferences, habits and "
    "recurring requirements suitable for a user profile. Project-limited lasting requirements "
    "may also be long_term; do not broaden their conditions. Project/business information "
    "such as suppliers and material locations is regular unless explicitly temporary. "
    "Absence of a deadline alone never implies long_term. Split statements with different "
    "lifetimes. Stage-specific responsibilities have value across tasks: extract their "
    "explicit business activity as a scenario, e.g. material review uses 材料审核; "
    "do not discard them solely because they are temporary. "
    "Do not extract single-task execution progress, tool results or completion "
    "notifications such as 'generating the file' or 'email sent'. Preserve uncertainty: "
    "a possible plan is not a definite future event. "
    "Preserve temporal meaning in self-contained text. Resolve stated dates relative to "
    "occurred_at only when timestamp_origin=source. adapter_observed is ingestion observation "
    "time, not the original event time: do not resolve relative dates or infer state recency "
    "from it. Such messages have occurred_at=null in model input. For example, '本周我负责 "
    "材料审核，下周恢复原安排' with unknown event time must produce NO fact and a "
    "context_insufficient assessment. Missing required dates must be context_insufficient. "
    "not processing time. Use ISO8601 timestamps with timezone; never fabricate precision. "
    "expired_at is only an explicitly stated deadline or historical interval boundary; "
    "never compute a default TTL. For 'valid this week', use the next Monday at 00:00 in "
    "the source message's timezone as the exclusive deadline. "
    "temporal_kind=historical requires an effective_at and expired_at interval. "
    "A year-only historical period uses Jan 1 through Jan 1 of the next year (exclusive). "
    "If a current move/update is stated only by year or month, set effective_at=null; "
    "do not invent the exact first day as the event date. "
    "change_kind is none, correction, update, or clarification; use clarification only "
    "for explicit "
    "resolution of contradictory prior statements, update only for an explicit "
    "current-state change. "
    "Unresolved references or missing necessary context yield no fact and a "
    "context_insufficient assessment; no durable value yields no_durable_value. "
    "For EACH target user message, return an assessment of evaluated, no_durable_value "
    "or context_insufficient with a short reason (no chain-of-thought). A message may have "
    "facts and also context_insufficient when another statement lacks context. "
    "Return JSON {facts:[{memory:string,message_ids:[string],business_domains:[labels]|null,"
    "project_limited:boolean,project_context:string|null,"
    "temporal_kind:current|historical,retention_kind:temporary|regular|long_term,"
    "change_kind:none|correction|update|clarification,effective_at:timestamp|null,"
    "expired_at:timestamp|null}],assessments:[{message_id:string,"
    "outcome:evaluated|no_durable_value|context_insufficient,reason:string}]}."
)

GOVERNANCE_PROMPT = (
    "Resolve a new user fact against same-subject candidates. First compare applicability: "
    "business labels are semantic scenario tags; different strings can mean the same scenario. "
    "Only compare facts with equivalent actual conditions. Different projects, a project-limited "
    "rule versus a general rule, or genuinely different business scenarios are ADD, never "
    "DUPLICATE, SUPERSEDE or DISPUTE. Preserve project conditions from memory text and "
    "project_context even though project IDs are null. "
    "Return JSON {action:ADD|DUPLICATE|SUPERSEDE|DISPUTE,memory_ids:[string],"
    "justification_message_id:string|null,justification_quote:string|null,histor"
    "ical_before_id:string|null}. "
    "ADD has no IDs. DUPLICATE has exactly one active ID and requires identical durable meaning "
    "and temporal applicability, not mere similarity. When supplied, "
    "candidate.explicit_expired_at is the stated temporal boundary; "
    "candidate.expired_at may instead be a system TTL and must not prevent DUPLICATE solely "
    "because the incoming fact has no stated deadline. Historical intervals that do not overlap "
    "are ADD, not conflict or duplicate. SUPERSEDE requires explicit user correction, "
    "current-state "
    "update, or clarification, with a verbatim target USER quote and its ID. It replaces an old "
    "current-state assertion, not unrelated, complementary or historical facts. A "
    "disputed candidate "
    "can only be superseded by explicit clarification; include all members of that conflict group. "
    "Action must match fact.change_kind: none forbids SUPERSEDE. A different birthday with "
    "change_kind=none is DISPUTE, not an implicit correction or update. "
    "Never prefer a statement just because it arrived later; compare occurred_at and effective_at. "
    "For a delayed current-state assertion about the SAME attribute that predates a "
    "newer candidate, "
    "use ADD and set historical_before_id to that newer ID. This bounds the delayed assertion "
    "as history, never as another unbounded current state. Use "
    "historical_before_id=null otherwise. "
    "Use DISPUTE for contradictory claims when time or authority cannot resolve them; include "
    "all affected same-scope candidates. Use only supplied IDs. Disputed candidates must never "
    "be silently reactivated or selected as duplicates. Default to ADD for compatible "
    "independent facts."
)

APPLICABILITY_PROMPT = (
    "Select memories applicable to the supplied task query and business scenario. "
    "Business labels are semantic hints, not exhaustive enums or authorization. "
    "Use memory text and project_context: a project-limited memory only applies when the "
    "query identifies that same project/context. Never broaden it because project IDs are null. "
    "Different projects or genuinely different scenarios must be excluded. A relevant "
    "general user fact can apply across scenarios. Do not select merely because topics overlap. "
    "Return JSON {memory_ids:[string]} using only supplied candidate IDs."
)

SCENE_PROMPT = (
    "Map each proposed business task label to an existing scene only when its definition "
    "covers the same activity. A subtype may reuse a broader scene, but a different activity "
    "must get a new short canonical name in the user's language and a precise definition. "
    "Do not broaden existing definitions. Project names, customers and objects are not scenes. "
    "Return JSON {decisions:[{label:string,name:string,definition:string|null}]} "
    "with one decision for every input label. Existing names require definition=null."
)

TASK_APPLICABILITY_PROMPT = (
    "Judge every candidate memory against the task query. Check task relevance, business "
    "applicability, project conditions, customer/recipient conditions and other conditions "
    "in the full memory text. Include relevant memories when conditions are unknown, preserving "
    "their full conditional text. Exclude clear mismatches. A more specific conflicting rule "
    "takes precedence only when the query explicitly satisfies its condition; keep unrelated "
    "requirements from a general memory. Do not change memory state. "
    "Return JSON {decisions:[{id:string,include:boolean,reason:string}]} with exactly one "
    "decision per supplied candidate. Order included decisions by task relevance and applicable "
    "specificity. Use only supplied IDs."
)


class ApplicabilityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_ids: list[str]


class ApplicabilityModel(Protocol):
    def select_applicable(
        self, *, query: str, business_domain: str, candidates: list[dict[str, Any]]
    ) -> list[str]: ...


def normalize_business_domain(label: str) -> str:
    """Normalize initial label aliases without restricting other semantic scenarios."""
    label = label.strip().lower()
    return {
        "通信": "communication",
        "通讯": "communication",
        "sms": "communication",
        "短信": "communication",
        "电话": "communication",
        "即时消息": "communication",
        "邮件": "email",
        "电子邮件": "email",
        "邮件处理": "email",
        "云盘": "disk",
        "个人云盘": "disk",
        "cloud_drive": "disk",
    }.get(label, label)


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: str = Field(min_length=1, max_length=4096)
    message_ids: list[str] = Field(min_length=1)
    business_domains: list[str] | None = None
    project_limited: bool = False
    project_context: str | None = Field(default=None, min_length=1, max_length=512)
    temporal_kind: Literal["current", "historical"] = "current"
    retention_kind: RetentionKind = "regular"
    change_kind: Literal["none", "correction", "update", "clarification"] = "none"
    effective_at: datetime | None = None
    expired_at: datetime | None = None

    @field_validator("business_domains")
    @classmethod
    def normalize_domains(cls, value):
        if value == []:
            raise ValueError("business_domains must not be empty")
        if value is None:
            return None
        normalized = [normalize_business_domain(label) for label in value]
        if any(not label or len(label) > 32 for label in normalized):
            raise ValueError("business labels must contain 1 to 32 characters")
        return sorted(set(normalized))

    @field_validator("effective_at", "expired_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("fact timestamps must have a timezone")
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def check_interval(self):
        if self.project_limited != bool(self.project_context and self.project_context.strip()):
            raise ValueError("project-limited facts require project_context")
        if self.project_context is not None:
            self.project_context = self.project_context.strip()
            if self.project_context not in self.memory:
                raise ValueError("memory text must preserve project_context")
        if self.effective_at and self.expired_at and self.expired_at <= self.effective_at:
            raise ValueError("expired_at must be after effective_at")
        return self


def candidate_skip_reason(fact: ExtractedFact) -> str | None:
    if fact.temporal_kind == "historical" and (
        fact.effective_at is None or fact.expired_at is None
    ):
        return "TIME_UNRESOLVED"
    return None


class ExtractionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    outcome: Literal["evaluated", "no_durable_value", "context_insufficient"]
    reason: str = Field(min_length=1, max_length=512)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[ExtractedFact]
    assessments: list[ExtractionAssessment] = Field(default_factory=list)


def validate_fact_evidence(
    facts: list[ExtractedFact], targets: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
) -> None:
    input_ids = {message["message_id"] for message in [*targets, *(history or [])]}
    user_ids = {message["message_id"] for message in targets if message["role"] == "user"}
    for fact in facts:
        cited = set(fact.message_ids)
        if not cited.issubset(input_ids):
            raise ProviderError("extraction output cites a non-target/non-history message_id")
        if not cited.intersection(user_ids):
            raise ProviderError("fact evidence must include a target user message")


class GovernanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["ADD", "DUPLICATE", "SUPERSEDE", "DISPUTE"]
    memory_ids: list[str] = Field(default_factory=list)
    justification_message_id: str | None = None
    justification_quote: str | None = None
    historical_before_id: str | None = None

    @model_validator(mode="after")
    def check_targets(self):
        if len(self.memory_ids) != len(set(self.memory_ids)):
            raise ValueError("governance IDs must be unique")
        if self.action == "ADD" and self.memory_ids:
            raise ValueError("ADD cannot reference existing memories")
        if self.historical_before_id is not None and self.action != "ADD":
            raise ValueError("historical boundary is only valid for ADD")
        if self.action == "DUPLICATE" and len(self.memory_ids) != 1:
            raise ValueError("DUPLICATE must reference exactly one memory")
        if self.action in {"SUPERSEDE", "DISPUTE"} and not self.memory_ids:
            raise ValueError("governance requires at least one existing memory")
        return self


class FactModel(Protocol):
    def extract(
        self, *, targets: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[ExtractedFact]: ...

    def resolve(
        self,
        *,
        fact: ExtractedFact,
        candidates: list[dict[str, Any]],
        targets: list[dict[str, Any]],
    ) -> GovernanceDecision: ...


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _parse_json_object(value: str) -> dict[str, Any]:
    stripped = value.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("model response must be a JSON object")
    return parsed


@dataclass
class QwenFactModel:
    api_key: str
    base_url: str
    model: str = "qwen3.8-max"
    output_tokens: int = 2048
    input_tokens: int = 12000

    @property
    def input_limit(self) -> int:
        return self.input_tokens - self.output_tokens

    def __post_init__(self) -> None:
        self._client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, timeout=120, max_retries=0
        )

    def _json_completion(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=self.output_tokens,
                extra_body={"enable_thinking": False},
            )
            usage = getattr(response, "usage", None)
            self.last_usage = usage.model_dump() if usage else None
            if response.choices[0].finish_reason == "length":
                raise OutputLimitError("model output was truncated")
            content = response.choices[0].message.content
            if not content:
                raise ProviderError("model returned empty content")
            return _parse_json_object(content)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Qwen request or output failed: {exc}") from exc

    def extract(
        self, *, targets: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[ExtractedFact]:
        return self.extract_with_outcomes(targets=targets, history=history).facts

    def extract_with_outcomes(
        self, *, targets: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> ExtractionOutput:
        try:
            def model_messages(messages):
                return [
                    {**message, "occurred_at": None}
                    if message.get("timestamp_origin") == "adapter_observed" else message
                    for message in messages
                ]

            raw = self._json_completion(EXTRACTION_PROMPT, {
                "history": model_messages(history), "targets": model_messages(targets),
            })
            required = {
                "business_domains",
                "project_limited",
                "project_context",
                "temporal_kind",
                "retention_kind",
                "change_kind",
                "effective_at",
                "expired_at",
            }
            output = ExtractionOutput.model_validate(raw)
            if any(not required.issubset(fact) for fact in raw["facts"]):
                raise ProviderError(
                    "extraction output is missing applicability, temporal or retention fields"
                )
        except ValidationError as exc:
            raise ProviderError(f"invalid extraction output: {exc}") from exc
        validate_fact_evidence(output.facts, targets, history)
        user_ids = {m["message_id"] for m in targets if m["role"] == "user"}
        if "assessments" in raw:
            ids = [a.message_id for a in output.assessments]
            if set(ids) != user_ids or len(ids) != len(set(ids)):
                raise ProviderError("assessments must cover every target user exactly once")
            cited_ids = {mid for fact in output.facts for mid in fact.message_ids}
            for assessment in output.assessments:
                if assessment.outcome == "no_durable_value" and assessment.message_id in cited_ids:
                    raise ProviderError("no_durable_value contradicts extracted facts")
        return output

    def select_applicable(
        self, *, query: str, business_domain: str, candidates: list[dict[str, Any]]
    ) -> list[str]:
        try:
            output = ApplicabilityOutput.model_validate(
                self._json_completion(
                    APPLICABILITY_PROMPT,
                    {
                        "query": query,
                        "business_domain": business_domain,
                        "candidates": candidates,
                    },
                )
            )
        except ValidationError as exc:
            raise ProviderError(f"invalid applicability output: {exc}") from exc
        if not set(output.memory_ids).issubset({m["id"] for m in candidates}):
            raise ProviderError("applicability output cites an unknown memory ID")
        return list(dict.fromkeys(output.memory_ids))

    def normalize_scenes(self, *, labels: list[str], scenes: list[dict[str, Any]]):
        from memory_cmic.business_scenes import SceneDecision

        raw = self._json_completion(SCENE_PROMPT, {"labels": labels, "scenes": scenes})
        values = raw.get("decisions")
        if not isinstance(values, list) or len(values) != len(labels):
            raise ProviderError("invalid scene normalization output")
        try:
            return [SceneDecision(**value) for value in values]
        except (TypeError, KeyError) as exc:
            raise ProviderError("invalid scene normalization output") from exc

    def select_for_query(self, *, query: str, candidates: list[dict[str, Any]]):
        raw = self._json_completion(
            TASK_APPLICABILITY_PROMPT, {"query": query, "candidates": candidates}
        )
        decisions = raw.get("decisions")
        if not isinstance(decisions, list) or len(decisions) != len(candidates):
            raise ProviderError("invalid task applicability output")
        ids = {candidate["id"] for candidate in candidates}
        if {item.get("id") for item in decisions if isinstance(item, dict)} != ids:
            raise ProviderError("task applicability output cites unknown or missing memory IDs")
        if any(
            not isinstance(item.get("include"), bool) or not isinstance(item.get("reason"), str)
            for item in decisions
        ):
            raise ProviderError("invalid task applicability decision")
        return decisions

    def resolve(
        self,
        *,
        fact: ExtractedFact,
        candidates: list[dict[str, Any]],
        targets: list[dict[str, Any]],
    ) -> GovernanceDecision:
        try:
            output = GovernanceDecision.model_validate(
                self._json_completion(
                    GOVERNANCE_PROMPT,
                    {
                        "fact": fact.model_dump(mode="json"),
                        "candidates": candidates,
                        "targets": targets,
                    },
                )
            )
        except ValidationError as exc:
            raise ProviderError(f"invalid governance output: {exc}") from exc
        referenced = set(output.memory_ids)
        if output.historical_before_id:
            referenced.add(output.historical_before_id)
        if not referenced.issubset({candidate["id"] for candidate in candidates}):
            raise ProviderError("governance output cites an unknown memory ID")
        return output


@dataclass
class SiliconFlowEmbedder:
    api_key: str
    base_url: str
    model_id: str = "BAAI/bge-m3"

    def __post_init__(self) -> None:
        self._client = OpenAI(
            api_key=self.api_key, base_url=self.base_url, timeout=30, max_retries=0
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            try:
                response = self._client.embeddings.create(
                    model=self.model_id, input=batch, encoding_format="float"
                )
            except Exception as exc:
                raise ProviderError(f"SiliconFlow embedding request failed: {exc}") from exc
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(list(item.embedding) for item in ordered)
        if len(vectors) != len(texts) or any(len(vector) != 1024 for vector in vectors):
            raise ProviderError("SiliconFlow returned an unexpected embedding shape")
        return vectors
