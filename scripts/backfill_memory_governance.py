"""Review stored profiles and backfill governance/profile work; dry-run by default."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_cmic.audit import record_audit
from memory_cmic.db import create_database_engine
from memory_cmic.governance_state import backfill_user, lock_user
from memory_cmic.models import MemoryEvidence, MemoryItem, ProfileProperty
from memory_cmic.profile import _snapshot, enqueue_profile_rebuild, read_profiles


def backfill(session: Session, tenant_id: str, user_id: str | None = None) -> list[dict]:
    users = set(
        session.scalars(
            select(MemoryItem.subject_id).where(
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.subject_type == "user",
            )
        )
    ) | set(
        session.scalars(
            select(ProfileProperty.user_id).where(
                ProfileProperty.tenant_id == tenant_id,
            )
        )
    )
    if user_id is not None:
        users &= {user_id}
    report = []
    for user in sorted(users):
        lock_user(session, tenant_id, user)
        valid_ids = {
            p.id
            for p in read_profiles(
                session,
                tenant_id=tenant_id,
                user_id=user,
                include_inferred=True,
            )
        }
        invalidated = []
        for profile in session.scalars(
            select(ProfileProperty).where(
                ProfileProperty.tenant_id == tenant_id,
                ProfileProperty.user_id == user,
                ProfileProperty.status == "active",
            )
        ):
            if profile.id in valid_ids:
                continue
            before = {"status": profile.status, "version": profile.version}
            profile.status = "invalidated"
            profile.version += 1
            profile.updated_at = datetime.now(UTC)
            for edge in session.scalars(
                select(MemoryEvidence).where(
                    MemoryEvidence.tenant_id == tenant_id,
                    MemoryEvidence.downstream_profile_id == profile.id,
                    MemoryEvidence.status == "active",
                )
            ):
                edge.status = "invalidated"
                edge.invalidated_at = profile.updated_at
            record_audit(
                session,
                tenant_id=tenant_id,
                action="INVALIDATE",
                target_type="profile",
                target_id=profile.id,
                reason_code="PROFILE_GOVERNANCE_BACKFILL",
                state_before=before,
                state_after={"status": profile.status, "version": profile.version},
            )
            invalidated.append(profile.id)
        session.flush()
        pending = backfill_user(session, tenant_id, user)
        fingerprint, candidates = _snapshot(session, tenant_id, user)
        task = enqueue_profile_rebuild(session, tenant_id=tenant_id, user_id=user)
        report.append(
            {
                "user_id": user,
                "invalidated_profile_ids": invalidated,
                "governance_pending_added": pending,
                "profile_candidates": len(candidates),
                "profile_input": fingerprint,
                "profile_task_id": task.id if task else None,
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = create_database_engine()
    try:
        with Session(engine) as session:
            records = backfill(session, args.tenant_id, args.user_id)
            if args.apply:
                session.commit()
            else:
                session.rollback()
        print(json.dumps({"applied": args.apply, "users": records}, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
