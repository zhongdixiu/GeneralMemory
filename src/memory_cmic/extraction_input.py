from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from memory_cmic.model_input import compact_json, extraction_size, model_message


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
    history = [m for m in history if m["content"].strip() or m.get("tool_calls")
               or m.get("attachments")]
    linked = {id(m) for m in required_history(targets, history)}
    support_ids = {mid for memory in related_memories for mid in memory["message_ids"]}
    pool = [m for index, m in enumerate(history) if index >= len(history) - 30
            or id(m) in linked or m["message_id"] in support_ids]
    base = extraction_size(system, targets, [], related_memories)
    if base > limit:
        return None
    # Each history message is serialized once per pass. Adding a message needs only
    # integer arithmetic; never serialize the growing request for every candidate.
    sizes = [len(compact_json(model_message(m)).encode()) for m in pool]
    overhead = len(',"history":[]')
    if not pool or base + overhead + sum(sizes) + len(pool) - 1 <= limit:
        chosen = pool
    else:
        shortened = []
        for index, message in enumerate(pool):
            if len(message["content"]) > 300:
                start = message.get("start_char", 0)
                message = {**message, "content": message["content"][:300],
                           "start_char": start, "end_char": start + 300, "truncated": True}
                sizes[index] = len(compact_json(model_message(message)).encode())
            shortened.append(message)
        priority = sorted(range(len(pool)), key=lambda i: (
            pool[i]["message_id"] in support_ids, id(pool[i]) in linked, i), reverse=True)
        selected = set()
        used = base
        for index in priority:
            cost = sizes[index] + (1 if selected else overhead)
            if used + cost <= limit:
                selected.add(index)
                used += cost
        chosen = [m for i, m in enumerate(shortened) if i in selected]
    # Optional state hints still require their full raw support. Discard the hint
    # instead of blocking the target or presenting an incomplete support group.
    complete_ids = {m["message_id"] for m in [*targets, *chosen] if not m.get("truncated")}
    if not support_ids.issubset(complete_ids):
        return None
    return chosen


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
        if message["role"] != "user":
            # Auxiliary messages cannot independently yield user facts. Keep their
            # entire source for later context, without spending extraction calls.
            auxiliary = {**message, "start_char": 0, "end_char": len(content)}
            if slices and slices[-1].get("context_only"):
                slices[-1]["targets"].append(auxiliary)
            else:
                slices.append({"targets": [auxiliary], "history": [], "split_depth": 0,
                               "context_only": True})
            prior.append(auxiliary)
            continue
        start = 0
        while start < len(content) or (not content and start == 0):
            minimal = {**message, "content": content[start:start + 1],
                       "start_char": start, "end_char": min(start + 1, len(content))}
            if extraction_size(system, [minimal], []) > limit:
                slices.append({
                    "targets": [{**message, "content": content[start:], "start_char": start,
                                 "end_char": len(content)}],
                    "history": [], "split_depth": 0,
                    "context_error": "TARGET_METADATA_BUDGET_EXCEEDED",
                })
                prior.append({**message, "start_char": 0, "end_char": len(content)})
                break
            # Long historical replies do not prevent extraction of a new target.
            # Reserve only the bounded overlap when splitting this target itself.
            required = [m for m in prior if m.get("continuation")]
            if extraction_size(system, [minimal], required) > limit:
                required = []
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
            full = {**message, "content": content[start:], "start_char": start,
                    "end_char": len(content)}
            end = (len(content) if extraction_size(system, [full], required, related) <= limit
                   else start)
            low, high = start + 1, (len(content) if end == start else start)
            while low <= high:
                midpoint = (low + high) // 2
                fragment = {**message, "content": content[start:midpoint],
                            "start_char": start, "end_char": midpoint}
                if extraction_size(system, [fragment], required, related) <= limit:
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
            if (slices and not slices[-1].get("context_error")
                    and not slices[-1].get("context_only")):
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
                prior.append({**message, "start_char": 0, "end_char": len(content)})
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
