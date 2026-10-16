"""Report old business labels, or apply a reviewed JSONL mapping.

Report mode only reads data. Before --apply, review and edit proposed values and
provide a definition for every newly registered custom scene.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory_cmic.audit import record_audit
from memory_cmic.business_scenes import INITIAL_SCENES
from memory_cmic.db import create_database_engine
from memory_cmic.models import (
    BusinessScene,
    MemoryEvidence,
    MemoryItem,
    ProfileProperty,
    SourceRecord,
)
from memory_cmic.providers import normalize_business_domain


def _proposal(labels: list[str]) -> tuple[list[str] | None, str, str | None]:
    proposed = [normalize_business_domain(label) for label in labels]
    if all(label in INITIAL_SCENES for label in proposed):
        return sorted(set(proposed)), "matched initial scene aliases", None
    return None, "full content and scene definition require review", "UNKNOWN_SCENE"


def build_report(session: Session, tenant_id: str | None = None) -> list[dict]:
    report = []
    for cls, target_type in ((MemoryItem, "memory"), (ProfileProperty, "profile")):
        statement = select(cls).order_by(cls.tenant_id, cls.id)
        if tenant_id is not None:
            statement = statement.where(cls.tenant_id == tenant_id)
        for row in session.scalars(statement):
            if row.business_domains is None:
                continue
            suggested, reason, unresolved = _proposal(row.business_domains)
            item = {
                "target_type": target_type,
                "tenant_id": row.tenant_id,
                "id": row.id,
                "version": row.version,
                "updated_at": row.updated_at.isoformat(),
                "status": row.status,
                "old": row.business_domains,
                "proposed": suggested,
                "label_map": {
                    label: normalize_business_domain(label)
                    for label in row.business_domains
                    if normalize_business_domain(label) in INITIAL_SCENES
                },
                "register_aliases": [],
                "reason": reason,
                "unresolved": unresolved,
                "definitions": {},
            }
            if target_type == "memory":
                item["summary"] = row.summary
                item["content"] = row.content
                item["sources"] = [
                    source.raw_content
                    for source in session.scalars(
                        select(SourceRecord)
                        .join(
                            MemoryEvidence,
                            (MemoryEvidence.tenant_id == SourceRecord.tenant_id)
                            & (MemoryEvidence.upstream_source_id == SourceRecord.id),
                        )
                        .where(
                            MemoryEvidence.tenant_id == row.tenant_id,
                            MemoryEvidence.downstream_memory_id == row.id,
                            MemoryEvidence.status == "active",
                        )
                        .limit(3)
                    )
                ]
            else:
                item["property_key"] = row.property_key
                item["property_value"] = row.property_value
            report.append(item)
    return report


def apply_report(session: Session, records: list[dict]) -> list[dict]:
    correlation_id = uuid4().hex
    outcomes = []
    alias_targets: dict[tuple[str, str], set[str]] = {}
    for item in records:
        proposed = item.get("proposed")
        if not proposed:
            continue
        for label in item["old"]:
            target = (item.get("label_map") or {}).get(label)
            if target is None and len(proposed) == 1:
                target = proposed[0]
            if target is not None:
                alias_targets.setdefault((item["tenant_id"], label.strip().lower()), set()).add(
                    target
                )
    tenant_id = None
    for item in sorted(
        records, key=lambda value: (value["tenant_id"], value["target_type"], value["id"])
    ):
        if item["tenant_id"] != tenant_id:
            tenant_id = item["tenant_id"]
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(f"scene:{tenant_id}", 0)))
            )
        cls = {"memory": MemoryItem, "profile": ProfileProperty}[item["target_type"]]
        row = session.scalars(
            select(cls).where(cls.id == item["id"]).with_for_update()
        ).one_or_none()
        if row is None or row.tenant_id != item["tenant_id"]:
            outcomes.append({"id": item["id"], "status": "MISSING"})
            continue
        proposed = item["proposed"]
        if proposed is None:
            outcomes.append({"id": row.id, "status": "UNRESOLVED"})
            continue
        if not proposed or any(not value or len(value) > 32 for value in proposed):
            raise ValueError(f"invalid proposed scene list for {row.id}")
        proposed = sorted(set(proposed))
        if row.business_domains == proposed:
            outcomes.append({"id": row.id, "status": "UNCHANGED"})
            continue
        if (
            row.version != item["version"]
            or row.updated_at.isoformat() != item["updated_at"]
            or row.status != item["status"]
            or row.business_domains != item["old"]
            or (
                item["target_type"] == "memory"
                and (row.content != item["content"] or row.summary != item["summary"])
            )
            or (
                item["target_type"] == "profile"
                and (
                    row.property_key != item["property_key"]
                    or row.property_value != item["property_value"]
                )
            )
        ):
            outcomes.append({"id": row.id, "status": "INPUT_CHANGED"})
            continue
        alias_map = dict(item.get("label_map") or {})
        if any(
            label not in proposed
            and not (len(item["old"]) == len(proposed) == 1)
            and alias_map.get(label, normalize_business_domain(label)) not in proposed
            for label in item["old"]
        ):
            outcomes.append({"id": row.id, "status": "ALIAS_MAPPING_REQUIRED"})
            continue
        aliases = {}
        alias_error = False
        for label in item.get("register_aliases", []):
            if label not in item["old"]:
                raise ValueError(f"alias {label} was not in old scope for {row.id}")
            target = alias_map.get(label, proposed[0] if len(proposed) == 1 else None)
            if target not in proposed:
                alias_error = True
                break
            aliases[label.strip().lower()] = target
        if alias_error:
            outcomes.append({"id": row.id, "status": "ALIAS_MAPPING_REQUIRED"})
            continue
        if any(len(alias_targets[(row.tenant_id, label)]) > 1 for label in aliases):
            outcomes.append({"id": row.id, "status": "ALIAS_CONFLICT"})
            continue
        existing_scenes = session.scalars(
            select(BusinessScene).where(BusinessScene.tenant_id == row.tenant_id)
        ).all()
        if any(
            name in {alias.strip().lower() for alias in scene.aliases}
            for name in proposed
            for scene in existing_scenes
            if scene.name != name
        ):
            outcomes.append({"id": row.id, "status": "SCENE_NAME_CONFLICT"})
            continue
        if any(
            label == scene.name or label in {value.strip().lower() for value in scene.aliases}
            for label, target in aliases.items()
            for scene in existing_scenes
            if scene.name != target
        ):
            outcomes.append({"id": row.id, "status": "ALIAS_CONFLICT"})
            continue
        if (
            item["target_type"] == "profile"
            and row.status == "active"
            and row.property_key is not None
        ):
            collision = session.scalars(
                select(ProfileProperty).where(
                    ProfileProperty.tenant_id == row.tenant_id,
                    ProfileProperty.user_id == row.user_id,
                    ProfileProperty.property_key == row.property_key,
                    ProfileProperty.business_domains == proposed,
                    ProfileProperty.status == "active",
                    ProfileProperty.id != row.id,
                )
            ).first()
            if collision:
                outcomes.append({"id": row.id, "status": "PROFILE_COLLISION"})
                continue
        missing = [
            name for name in proposed if session.get(BusinessScene, (row.tenant_id, name)) is None
        ]
        if any(
            name not in item.get("definitions", {}) and name not in INITIAL_SCENES
            for name in missing
        ):
            outcomes.append({"id": row.id, "status": "SCENE_DEFINITION_REQUIRED"})
            continue
        for name in missing:
            definition = (
                INITIAL_SCENES[name][0] if name in INITIAL_SCENES else item["definitions"][name]
            )
            if not definition or len(definition) > 512:
                raise ValueError(f"invalid definition for {name}")
            session.add(
                BusinessScene(
                    tenant_id=row.tenant_id,
                    name=name,
                    definition=definition,
                    aliases=INITIAL_SCENES[name][1] if name in INITIAL_SCENES else [],
                )
            )
        session.flush()
        for label, name in aliases.items():
            scene = session.get(BusinessScene, (row.tenant_id, name))
            if label != name and label not in scene.aliases:
                scene.aliases = sorted({*scene.aliases, label})
        before = {"business_domains": row.business_domains, "version": row.version}
        row.business_domains = proposed
        row.version += 1
        row.updated_at = datetime.now(UTC)
        record_audit(
            session,
            tenant_id=row.tenant_id,
            action="MIGRATE",
            target_type=item["target_type"],
            target_id=row.id,
            operator_type="admin",
            operator_id="business_scene_migration",
            reason_code="BUSINESS_SCENE_NORMALIZED",
            reason=item["reason"][:512],
        correlation_id=correlation_id,
            state_before=before,
            state_after={"business_domains": proposed, "version": row.version},
        )
        outcomes.append({"id": row.id, "status": "APPLIED"})
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--tenant-id")
    args = parser.parse_args()
    if args.report and args.apply:
        parser.error("choose --report or --apply")
    engine = create_database_engine()
    if not args.apply:
        output = args.report or Path("/tmp/memory_cmic_business_scene_migration.jsonl")
        with Session(engine) as session:
            records = build_report(session, tenant_id=args.tenant_id)
        output.write_text(
            "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in records)
        )
        print(json.dumps({"reported": len(records), "path": str(output)}))
    else:
        records = [json.loads(line) for line in args.apply.read_text().splitlines() if line]
        with Session(engine) as session, session.begin():
            outcomes = apply_report(session, records)
        print(json.dumps(outcomes, ensure_ascii=False))


if __name__ == "__main__":
    main()
