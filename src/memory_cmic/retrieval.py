from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, String, func, literal, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Session

from memory_cmic.evidence import memory_has_valid_evidence
from memory_cmic.extraction_input import input_size
from memory_cmic.models import (
    BusinessScene,
    MemoryEmbedding,
    MemoryEvidence,
    MemoryItem,
    MemoryTask,
    MemoryTaskSource,
    SourceRecord,
)
from memory_cmic.providers import (
    TASK_APPLICABILITY_PROMPT,
    TASK_SELECTION_PROMPT,
    ApplicabilityModel,
    Embedder,
    ProviderError,
    normalize_business_domain,
)


def _scope_filters(
    *,
    tenant_id: str,
    business_domain: str,
    project_id: str | None,
    now: datetime,
    semantic: bool = False,
) -> tuple[object, ...]:
    project_filter = MemoryItem.project_domains.is_(None)
    if project_id is not None:
        project_filter = or_(project_filter, MemoryItem.project_domains.any(project_id))

    return (
        MemoryItem.tenant_id == tenant_id,
        MemoryItem.status == "active",
        MemoryItem.effective_at <= now,
        or_(MemoryItem.expired_at.is_(None), MemoryItem.expired_at > now),
        literal(True)
        if semantic
        else or_(
            MemoryItem.business_domains.is_(None),
            MemoryItem.business_domains.any(normalize_business_domain(business_domain)),
        ),
        project_filter,
        literal(True)
        if semantic
        else or_(
            MemoryItem.metadata_json["project_context"].as_string().is_(None),
            MemoryItem.project_domains.is_not(None),
        ),
    )


def _subject_routes(
    *,
    user_id: str,
    authorized_agent_ids: list[str],
    company_ids: list[str],
) -> list[tuple[object, ...]]:
    routes = [(MemoryItem.subject_type == "user", MemoryItem.subject_id == user_id)]
    if authorized_agent_ids:
        routes.append(
            (
                MemoryItem.subject_type == "agent",
                MemoryItem.subject_id.in_(authorized_agent_ids),
            )
        )
    if company_ids:
        routes.append(
            (MemoryItem.subject_type == "company", MemoryItem.subject_id.in_(company_ids))
        )
    return routes


def _current_content_hash() -> object:
    memory_text = func.concat(MemoryItem.summary, literal("\n"), MemoryItem.content)
    return func.encode(func.sha256(func.convert_to(memory_text, "UTF8")), "hex")


def _vector_statement(
    *,
    route: tuple[object, ...],
    scope_filters: tuple[object, ...],
    model_id: str,
    query_embedding: list[float],
    limit: int,
) -> Select[tuple[MemoryItem, float]]:
    distance = MemoryEmbedding.embedding.cosine_distance(query_embedding).label("distance")
    return (
        select(MemoryItem, distance)
        .join(
            MemoryEmbedding,
            (MemoryEmbedding.tenant_id == MemoryItem.tenant_id)
            & (MemoryEmbedding.memory_id == MemoryItem.id),
        )
        .where(
            *scope_filters,
            *route,
            MemoryEmbedding.model_id == model_id,
            MemoryEmbedding.status == "active",
            MemoryEmbedding.content_hash == _current_content_hash(),
        )
        .order_by(distance)
        .limit(limit)
    )


def search_memories(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    business_domain: str,
    project_id: str | None,
    authorized_agent_ids: list[str],
    company_ids: list[str],
    model_id: str | None = None,
    query_embedding: list[float] | None = None,
    limit: int = 10,
    now: datetime | None = None,
    query: str | None = None,
    applicability_model: ApplicabilityModel | None = None,
) -> list[MemoryItem]:
    """Return eligible user, Agent and company memories, optionally vector-ranked."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if query_embedding is not None and model_id is None:
        raise ValueError("model_id is required for vector search")
    if (query is None) != (applicability_model is None) or (
        query is not None and not query.strip()
    ):
        raise ValueError("semantic applicability requires a nonempty query and applicability_model")

    def applicable(memories: list[MemoryItem]) -> list[MemoryItem]:
        if applicability_model is None or not memories:
            return memories[:limit]
        ids = applicability_model.select_applicable(
            query=query,
            business_domain=normalize_business_domain(business_domain),
            candidates=[
                {
                    "id": m.id,
                    "memory": m.content,
                    "business_domains": m.business_domains,
                    "project_context": (m.metadata_json or {}).get("project_context"),
                }
                for m in memories
            ],
        )
        if not set(ids).issubset({m.id for m in memories}):
            raise ProviderError("applicability output cites an unknown memory ID")
        return [m for m in memories if m.id in ids][:limit]

    candidate_limit = max(30, limit * 3) if applicability_model is not None else limit

    scope_filters = _scope_filters(
        tenant_id=tenant_id,
        business_domain=business_domain,
        project_id=project_id,
        now=now or datetime.now(UTC),
        semantic=applicability_model is not None,
    )
    routes = _subject_routes(
        user_id=user_id,
        authorized_agent_ids=authorized_agent_ids,
        company_ids=company_ids,
    )

    if query_embedding is None:
        memories: dict[str, MemoryItem] = {}
        for route in routes:
            statement = select(MemoryItem).where(*scope_filters, *route).order_by(MemoryItem.id)
            for memory in session.scalars(statement):
                memories[memory.id] = memory
        ordered = [memories[memory_id] for memory_id in sorted(memories)]
        # Preserve the existing unranked listing behavior for exact scope callers.
        return applicable(ordered[:candidate_limit]) if applicability_model is not None else ordered

    ranked: dict[str, tuple[MemoryItem, float]] = {}
    for route in routes:
        statement = _vector_statement(
            route=route,
            scope_filters=scope_filters,
            model_id=model_id,
            query_embedding=query_embedding,
            limit=candidate_limit,
        )
        for memory, distance in session.execute(statement):
            previous = ranked.get(memory.id)
            if previous is None or distance < previous[1]:
                ranked[memory.id] = (memory, distance)

    ordered = sorted(ranked.values(), key=lambda item: (item[1], item[0].id))
    return applicable([memory for memory, _ in ordered[:candidate_limit]])


def _matching_source_ids(
    session: Session, *, tenant_id: str, user_id: str,
    agent_id: str | None, source_system: str | None,
) -> set[str]:
    statement = (
        select(SourceRecord.id)
        .join(MemoryEvidence, (MemoryEvidence.tenant_id == SourceRecord.tenant_id)
              & (MemoryEvidence.upstream_source_id == SourceRecord.id))
        .join(MemoryItem, (MemoryItem.tenant_id == MemoryEvidence.tenant_id)
              & (MemoryItem.id == MemoryEvidence.downstream_memory_id))
        .where(
            SourceRecord.tenant_id == tenant_id,
            MemoryItem.subject_type == "user",
            MemoryItem.subject_id == user_id,
            MemoryItem.cognitive_type == "fact",
        )
        .distinct()
    )
    if source_system is not None:
        statement = statement.where(SourceRecord.source_system == source_system)
    if agent_id is not None:
        submitted = (
            select(MemoryTaskSource.source_id)
            .join(MemoryTask, (MemoryTask.tenant_id == MemoryTaskSource.tenant_id)
                  & (MemoryTask.id == MemoryTaskSource.task_id))
            .where(
                MemoryTaskSource.tenant_id == tenant_id,
                MemoryTaskSource.source_id == SourceRecord.id,
                MemoryTaskSource.source_version == SourceRecord.version,
                or_(
                    MemoryTaskSource.kind == "target",
                    (MemoryTaskSource.kind == "history")
                    & (SourceRecord.agent_id == agent_id),
                ),
                MemoryTask.task_type == "fact_extract",
                MemoryTask.user_id == user_id,
                MemoryTask.agent_id == agent_id,
            )
            .exists()
        )
        statement = statement.where(
            submitted,
            or_(SourceRecord.agent_id.is_(None), SourceRecord.agent_id == agent_id),
        )
    return set(session.scalars(statement))


def search_task_memories(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    query: str,
    authorized_agent_ids: list[str],
    company_ids: list[str],
    embedder: Embedder,
    applicability_model: object | None = None,
    project_id: str | None = None,
    limit: int = 10,
    now: datetime | None = None,
    trace: list[dict] | None = None,
    validate_user_evidence: bool = False,
    agent_id: str | None = None,
    source_system: str | None = None,
) -> list[MemoryItem]:
    """Search a task without caller supplied business labels."""
    if not query.strip() or limit <= 0:
        raise ValueError("query must be nonempty and limit positive")
    if applicability_model is not None and not hasattr(applicability_model, "select_for_query"):
        raise ValueError("applicability_model must support task queries")
    source_filtered = agent_id is not None or source_system is not None
    validate_user_evidence = validate_user_evidence or source_filtered
    allowed_source_ids = _matching_source_ids(
        session, tenant_id=tenant_id, user_id=user_id,
        agent_id=agent_id, source_system=source_system,
    ) if source_filtered else None
    if source_filtered and not allowed_source_ids:
        return []
    stale = session.scalars(
        select(BusinessScene).where(
            BusinessScene.tenant_id == tenant_id,
            or_(
                BusinessScene.embedding.is_(None),
                BusinessScene.model_id.is_distinct_from(embedder.model_id),
            ),
        )
    ).all()
    vectors = embedder.embed([query, *[scene.definition for scene in stale]])
    query_vector = vectors[0]
    if stale:
        for scene, vector in zip(stale, vectors[1:], strict=True):
            scene.embedding = vector
            scene.model_id = embedder.model_id
        session.flush()
    scene_distance = BusinessScene.embedding.cosine_distance(query_vector)
    scene_names = session.scalars(
        select(BusinessScene.name)
        .where(BusinessScene.tenant_id == tenant_id, BusinessScene.model_id == embedder.model_id)
        .order_by(scene_distance)
        .limit(3)
    ).all()
    routes = _subject_routes(
        user_id=user_id,
        authorized_agent_ids=authorized_agent_ids,
        company_ids=company_ids,
    )
    base = _scope_filters(
        tenant_id=tenant_id,
        business_domain="",
        project_id=project_id,
        now=now or datetime.now(UTC),
        semantic=True,
    )
    if validate_user_evidence:
        base = (
            *base,
            MemoryItem.subject_type == "user",
            MemoryItem.cognitive_type.in_(["fact", "inference"]),
        )
    if source_filtered:
        checked: dict[str, bool] = {}
        check_time = now or datetime.now(UTC)
        memories = session.scalars(select(MemoryItem).where(
            *base, MemoryItem.subject_id == user_id,
        )).all()
        matching_ids = [memory.id for memory in memories if memory_has_valid_evidence(
            session, memory, tenant_id=tenant_id, user_id=user_id, now=check_time,
            checked=checked, allowed_source_ids=allowed_source_ids,
        )]
        base = (*base, MemoryItem.id.in_(matching_ids))
    candidate_limit = max(30, limit * 3)
    ranked: dict[str, tuple[MemoryItem, float, dict[str, int]]] = {}
    for channel in ("scene", "semantic"):
        channel_rows: dict[str, tuple[MemoryItem, float]] = {}
        filters = base
        if channel == "scene":
            filters = (
                *base,
                or_(
                    MemoryItem.business_domains.is_(None),
                    MemoryItem.business_domains.op("&&")(array(scene_names, type_=String(32))),
                ),
            )
        for route in routes:
            statement = _vector_statement(
                route=route,
                scope_filters=filters,
                model_id=embedder.model_id,
                query_embedding=query_vector,
                limit=candidate_limit,
            )
            for memory, distance in session.execute(statement):
                previous = channel_rows.get(memory.id)
                if previous is None or float(distance) < previous[1]:
                    channel_rows[memory.id] = (memory, float(distance))
        selected = sorted(channel_rows.values(), key=lambda row: (row[1], row[0].id))[
            : max(1, candidate_limit // 2)
        ]
        for position, (memory, distance) in enumerate(selected, start=1):
            previous = ranked.get(memory.id)
            if previous is None:
                ranked[memory.id] = (memory, distance, {channel: position})
            else:
                previous[2][channel] = position
    ordered = sorted(ranked.values(), key=lambda row: (row[1], row[0].id))
    if validate_user_evidence:
        checked: dict[str, bool] = {}
        check_time = now or datetime.now(UTC)
        ordered = [row for row in ordered if memory_has_valid_evidence(
            session, row[0], tenant_id=tenant_id, user_id=user_id, now=check_time,
            checked=checked, allowed_source_ids=allowed_source_ids,
        )]
    snapshots = {
        memory.id: (memory.version, memory.summary, memory.content) for memory, _, _ in ordered
    } if validate_user_evidence else {}
    selected = [memory for memory, _, _ in ordered]
    if applicability_model is not None and ordered:
        selected = _select_task_candidates(
            applicability_model, query=query, ordered=ordered, ranked=ranked, trace=trace,
        )
    if validate_user_evidence and selected:
        selected_ids = [memory.id for memory in selected]
        session.expire_all()
        if source_filtered:
            allowed_source_ids = _matching_source_ids(
                session, tenant_id=tenant_id, user_id=user_id,
                agent_id=agent_id, source_system=source_system,
            )
        checked = {}
        check_time = now or datetime.now(UTC)
        current = []
        for memory_id in selected_ids:
            memory = session.get(MemoryItem, memory_id)
            if memory is not None and (
                memory.version, memory.summary, memory.content
            ) == snapshots[memory_id]:
                if memory_has_valid_evidence(
                    session, memory, tenant_id=tenant_id, user_id=user_id,
                    now=check_time, checked=checked,
                    allowed_source_ids=allowed_source_ids,
                ):
                    current.append(memory)
                    if len(current) == limit:
                        break
        selected = current
    return selected[:limit]


def _select_task_candidates(
    applicability_model: object,
    *,
    query: str,
    ordered: list[tuple[MemoryItem, float, dict[str, int]]],
    ranked: dict[str, tuple[MemoryItem, float, dict[str, int]]],
    trace: list[dict] | None,
) -> list[MemoryItem]:
    # Keep both candidate channels; only omit model explanations when no trace is requested.
    payload = [
        {
            "id": memory.id,
            "memory": memory.content,
            "business_domains": memory.business_domains,
            "project_context": (memory.metadata_json or {}).get("project_context"),
            "source_positions": positions,
        }
        for memory, _, positions in ordered
    ]
    input_limit = getattr(applicability_model, "input_limit", 9952)
    compact = trace is None and hasattr(applicability_model, "select_for_query_ids")
    prompt = TASK_SELECTION_PROMPT if compact else TASK_APPLICABILITY_PROMPT
    if input_size(prompt, {"query": query, "candidates": payload}) > input_limit:
        raise ProviderError("task candidates exceed applicability input budget")
    by_id = {memory.id: memory for memory, _, _ in ordered}
    if compact:
        ids = applicability_model.select_for_query_ids(query=query, candidates=payload)
        if not set(ids).issubset(by_id) or len(ids) != len(set(ids)):
            raise ProviderError("task applicability output cites unknown or duplicate memory IDs")
        return [by_id[memory_id] for memory_id in ids]
    decisions = applicability_model.select_for_query(query=query, candidates=payload)
    if {item.get("id") for item in decisions} != set(by_id):
        raise ProviderError("task applicability output cites unknown or missing memory IDs")
    if trace is not None:
        trace.extend(
            {
                "id": item["id"],
                "sources": sorted(ranked[item["id"]][2]),
                "source_positions": ranked[item["id"]][2],
                "include": item["include"],
                "reason": item["reason"],
            }
            for item in decisions
        )
    return [by_id[item["id"]] for item in decisions if item["include"]]
