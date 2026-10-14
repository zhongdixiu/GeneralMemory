"""Model-visible Add payloads shared by planning and sending."""

from __future__ import annotations

import json
from typing import Any


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def request_size(system: str, payload: dict[str, Any]) -> int:
    # Compatibility budget: conservative UTF-8 bytes, NOT an exact token count.
    return len(system.encode()) + len(compact_json(payload).encode()) + 512


def model_message(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("timestamp_origin") == "adapter_observed" or not message.get("occurred_at"):
        return {key: value for key, value in {**message, "occurred_at": None}.items()
                if key != "received_at"}
    return message


def extraction_payload(targets, history, related_memories=None):
    return {
        "targets": [model_message(m) for m in targets],
        **({"history": [model_message(m) for m in history]} if history else {}),
        **({"related_memories": related_memories} if related_memories else {}),
        "timezone": "Asia/Shanghai",
    }


def extraction_size(system, targets, history, related_memories=None):
    return request_size(system, extraction_payload(targets, history, related_memories))


def _common_fields(items, *, exclude=()):
    if not items:
        return {}
    return {key: value for key, value in items[0].items()
            if key not in exclude and all(key in item and item[key] == value for item in items)}


def governance_payload(fact, candidates, targets):
    cited_ids = set(fact["message_ids"])
    rows = []
    for index, candidate in enumerate(candidates):
        row = {key: value for key, value in candidate.items()
               if key not in ("source_message_ids", "expired_at")}
        row["id"] = f"m{index}"
        if (not candidate.get("source_occurred_at")
                or candidate.get("timestamp_origin") == "adapter_observed"):
            row["shared_source"] = bool(cited_ids.intersection(
                candidate.get("source_message_ids") or []))
        rows.append(row)
    orders = [row["statement_order"] for row in rows
              if isinstance(row.get("statement_order"), dict)]
    order_defaults = _common_fields(orders)
    for row in rows:
        if isinstance(row.get("statement_order"), dict):
            row["statement_order"] = {key: value for key, value in row["statement_order"].items()
                                      if key not in order_defaults}
    defaults = _common_fields(rows, exclude=("id", "memory"))
    return {
        "fact": {key: value for key, value in fact.items()
                 if key not in ("project_limited", "retention_kind")},
        "candidates": [{key: value for key, value in row.items() if key not in defaults}
                       for row in rows],
        **({"candidate_defaults": defaults} if defaults else {}),
        **({"statement_order_defaults": order_defaults} if order_defaults else {}),
        "targets": [
            {key: value for key, value in model_message(message).items()
             if key in ("message_id", "role", "content", "occurred_at",
                        "timestamp_origin", "statement_order")}
            for message in targets
            if message["role"] == "user" and message["message_id"] in cited_ids
        ],
    }
