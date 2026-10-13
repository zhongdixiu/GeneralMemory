from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractionBudget:
    input_tokens: int = 12000
    output_tokens: int = 2048
    max_model_calls: int = 20

    def __post_init__(self):
        if self.input_tokens <= self.output_tokens or self.output_tokens <= 0:
            raise ValueError("input budget must exceed the positive output reserve")
        if self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")


def input_size(system: str, payload: dict[str, Any]) -> int:
    # Count UTF-8 bytes conservatively, rather than assuming a characters/token ratio.
    return len(system.encode()) + len(json.dumps(payload, ensure_ascii=False).encode()) + 512


def required_history(targets, history):
    selected = set()
    pending = list(targets)
    while pending:
        replies = {m.get("reply_to_message_id") for m in pending} - {None}
        turns = {m.get("turn_id") for m in pending} - {None}
        calls = {m.get("tool_call_id") for m in pending} - {None}
        calls.update(c["tool_call_id"] for m in pending for c in m.get("tool_calls", []))
        pending = []
        for index, message in enumerate(history):
            if index in selected:
                continue
            linked = (
                message["message_id"] in replies
                or (message.get("turn_id") is not None and message.get("turn_id") in turns)
                or (message.get("tool_call_id") is not None
                    and message.get("tool_call_id") in calls)
                or any(c["tool_call_id"] in calls for c in message.get("tool_calls", []))
                or (message.get("continuation", False)
                    and message["message_id"] in {m["message_id"] for m in targets})
            )
            if linked:
                selected.add(index)
                pending.append(message)
    return [message for index, message in enumerate(history) if index in selected]


def select_history(targets, history, *, system, limit, related_memories=None):
    related_memories = related_memories or []
    required = required_history(targets, history)
    support_ids = {mid for memory in related_memories for mid in memory["message_ids"]}
    required = [m for m in history if m in required or m["message_id"] in support_ids]
    extra = {"related_memories": related_memories} if related_memories else {}
    chosen = {id(m) for m in required}
    if input_size(system, {"targets": targets, "history": required, **extra}) > limit:
        return None
    for message in reversed(history[-30:]):
        proposed = [m for m in history if id(m) in chosen or m is message]
        if input_size(system, {"targets": targets, "history": proposed, **extra}) <= limit:
            chosen.add(id(message))
    return [m for m in history if id(m) in chosen]


def build_slices(
    targets: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    system: str,
    budget: ExtractionBudget,
    related_memories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    limit = budget.input_tokens - budget.output_tokens
    slices = []
    prior = list(history)
    related_memories = related_memories or []
    for message in targets:
        # Repeated/previous messages may supply context, never a duplicate target identity.
        prior = [m for m in prior if m["message_id"] != message["message_id"]]
        content = message["content"]
        start = 0
        while start < len(content) or (not content and start == 0):
            minimal = {**message, "content": content[start:start + 1],
                       "start_char": start, "end_char": min(start + 1, len(content))}
            if input_size(system, {"targets": [minimal], "history": []}) > limit:
                raise ValueError("message metadata cannot fit in the model input budget")
            required = required_history([minimal], prior)
            if input_size(system, {"targets": [minimal], "history": required}) > limit:
                slices.append({
                    "targets": [{**message, "content": content[start:], "start_char": start,
                                 "end_char": len(content)}],
                    "history": [], "split_depth": 0, "context_error": "CONTEXT_BUDGET_EXCEEDED",
                })
                break
            # Raw evidence takes priority over optional state hints. Keep complete support groups.
            related = []
            for memory in related_memories:
                proposed = [*related, memory]
                full = {**message, "content": content[start:], "start_char": start,
                        "end_char": len(content)}
                if select_history([full], prior, system=system, limit=limit,
                                  related_memories=proposed) is not None:
                    related = proposed
            support_ids = {mid for memory in related for mid in memory["message_ids"]}
            required = [m for m in prior if m in required or m["message_id"] in support_ids]
            extra = {"related_memories": related} if related else {}
            low, high, end = start + 1, len(content), start
            while low <= high:
                midpoint = (low + high) // 2
                fragment = {**message, "content": content[start:midpoint],
                            "start_char": start, "end_char": midpoint}
                if input_size(system, {"targets": [fragment],
                                       "history": required, **extra}) <= limit:
                    end, low = midpoint, midpoint + 1
                else:
                    high = midpoint - 1
            if end < len(content):
                paragraph = content.rfind("\n", start, end)
                if paragraph > start:
                    end = paragraph + 1
            fragment = {**message, "content": content[start:end],
                        "start_char": start, "end_char": end}
            selected = select_history([fragment], prior, system=system, limit=limit,
                                      related_memories=related)
            current = {"targets": [fragment], "history": selected, "split_depth": 0, **extra}
            if slices and not slices[-1].get("context_error"):
                previous = slices[-1]
                if message["message_id"] not in {m["message_id"] for m in previous["targets"]}:
                    merged = [*previous["targets"], fragment]
                    pool = [m for m in prior
                            if m["message_id"] not in {t["message_id"] for t in merged}]
                    merged_related = [m for m in related
                                      if all(mid in {h["message_id"] for h in pool + merged}
                                             for mid in m["message_ids"])]
                    merged_history = select_history(merged, pool, system=system, limit=limit,
                                                    related_memories=merged_related)
                    if merged_history is not None:
                        previous.update(targets=merged, history=merged_history,
                                        related_memories=merged_related)
                    else:
                        slices.append(current)
                else:
                    slices.append(current)
            else:
                slices.append(current)
            if not content:
                prior.append(fragment)
                break
            # Keep an exact, bounded overlap to understand the next long-message fragment.
            prior = [m for m in prior if not m.get("continuation")]
            if end < len(content):
                overlap_start = max(start, end - 256)
                prior.append({**fragment, "content": content[overlap_start:end],
                              "start_char": overlap_start, "continuation": True})
            else:
                prior.append(fragment)
            start = end
    return slices


def split_slice(current: dict[str, Any]) -> list[dict[str, Any]]:
    if current["split_depth"] >= 2:
        raise ValueError("output truncation recovery exhausted")
    targets = current["targets"]
    if len(targets) > 1:
        midpoint = len(targets) // 2
        first_targets, second_targets = targets[:midpoint], targets[midpoint:]
    else:
        target = targets[0]
        content = target["content"]
        if len(content) < 2:
            raise ValueError("target fragment cannot be split further")
        midpoint = len(content) // 2
        start = target["start_char"]
        first_targets = [{**target, "content": content[:midpoint], "end_char": start + midpoint}]
        second_targets = [{**target, "content": content[midpoint:], "start_char": start + midpoint}]
    return [
        {**current, "targets": first_targets, "split_depth": current["split_depth"] + 1},
        {
            **current,
            "targets": second_targets,
            "history": [*current["history"], *first_targets],
            "split_depth": current["split_depth"] + 1,
        },
    ]
