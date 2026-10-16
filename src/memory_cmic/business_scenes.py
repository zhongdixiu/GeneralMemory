from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from memory_cmic.extraction_input import ExtractionBudget, input_size
from memory_cmic.models import BusinessScene
from memory_cmic.providers import (
    SCENE_PROMPT,
    Embedder,
    ExtractedFact,
    ProviderError,
    normalize_business_domain,
)

INITIAL_SCENES = {
    "email": ("邮件的阅读、撰写、回复、处理和整理", ["邮件", "电子邮件", "邮件处理"]),
    "disk": (
        "个人云盘文件的备份、存储、整理和共享，不含本地文件",
        ["云盘", "个人云盘", "cloud_drive"],
    ),
    "communication": (
        "非邮件通信，包括短信、电话和即时消息",
        ["通信", "通讯", "sms", "短信", "电话", "即时消息"],
    ),
}


@dataclass(frozen=True)
class SceneDecision:
    label: str
    name: str
    definition: str | None = None


def scene_catalog(session: Session, tenant_id: str) -> list[BusinessScene]:
    return session.scalars(
        select(BusinessScene)
        .where(BusinessScene.tenant_id == tenant_id)
        .order_by(BusinessScene.name)
    ).all()


def _match(label: str, scenes: list[BusinessScene]) -> str | None:
    value = normalize_business_domain(label)
    for scene in scenes:
        if value == scene.name or value in {alias.strip().lower() for alias in scene.aliases}:
            return scene.name
    return None


def _seed(session: Session, tenant_id: str) -> None:
    existing = {scene.name for scene in scene_catalog(session, tenant_id)}
    for name, (definition, aliases) in INITIAL_SCENES.items():
        if name not in existing:
            session.add(
                BusinessScene(
                    tenant_id=tenant_id, name=name, definition=definition, aliases=aliases
                )
            )
    session.flush()


def normalize_fact_scenes(
    session_factory: sessionmaker[Session],
    *,
    tenant_id: str,
    fact: ExtractedFact,
    model: object,
    embedder: Embedder,
    consume_call,
    budget: ExtractionBudget,
) -> ExtractedFact:
    with session_factory.begin() as session:
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"scene:{tenant_id}", 0)))
        )
        _seed(session, tenant_id)
    if fact.business_domains is None:
        return fact
    for _ in range(3):
        with session_factory() as session:
            scenes = scene_catalog(session, tenant_id)
            known = {label: _match(label, scenes) for label in fact.business_domains}
            unknown = [label for label, name in known.items() if name is None]
            snapshot = [(scene.name, scene.definition, tuple(scene.aliases)) for scene in scenes]
            definitions = [
                {"name": scene.name, "definition": scene.definition, "aliases": scene.aliases}
                for scene in scenes
            ]
        decisions = []
        if unknown:
            if hasattr(model, "normalize_scenes"):
                if input_size(SCENE_PROMPT, {"labels": unknown, "scenes": definitions}) > (
                    budget.input_tokens - budget.output_tokens
                ):
                    raise ProviderError("scene directory exceeds model input budget")
                consume_call()
                decisions = model.normalize_scenes(labels=unknown, scenes=definitions)
            else:
                decisions = [
                    SceneDecision(label=label, name=label, definition=label) for label in unknown
                ]
            if {decision.label for decision in decisions} != set(unknown):
                raise ProviderError("scene normalization must decide every unknown label")
        mapped = {label: name for label, name in known.items() if name is not None}
        new = {}
        for decision in decisions:
            name = normalize_business_domain(decision.name)
            if not name or len(name) > 32:
                raise ProviderError("invalid canonical scene name")
            existing = next((scene for scene in definitions if scene["name"] == name), None)
            if existing is None:
                if not decision.definition or len(decision.definition) > 512:
                    raise ProviderError("new scene requires a concise definition")
                if name in new and new[name] != decision.definition:
                    raise ProviderError("conflicting new scene definitions")
                new[name] = decision.definition
            mapped[decision.label] = name
        vectors = {}
        if new:
            consume_call()
            vectors = dict(zip(new, embedder.embed(list(new.values())), strict=True))
        with session_factory.begin() as session:
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(f"scene:{tenant_id}", 0)))
            )
            current = scene_catalog(session, tenant_id)
            current_snapshot = [
                (scene.name, scene.definition, tuple(scene.aliases)) for scene in current
            ]
            if current_snapshot != snapshot:
                continue
            for name, definition in new.items():
                if _match(name, current) is not None:
                    raise ProviderError("new scene conflicts with an existing alias")
                scene = BusinessScene(
                    tenant_id=tenant_id,
                    name=name,
                    definition=definition,
                    aliases=sorted(
                        label
                        for label, target in mapped.items()
                        if target == name and label != name
                    ),
                    embedding=vectors[name],
                    model_id=embedder.model_id,
                )
                session.add(scene)
                current.append(scene)
            for label, name in mapped.items():
                scene = next(scene for scene in current if scene.name == name)
                if label != name and label not in scene.aliases:
                    scene.aliases = sorted({*scene.aliases, label})
        return fact.model_copy(update={"business_domains": sorted(set(mapped.values()))})
    raise ProviderError("scene directory changed during normalization")
