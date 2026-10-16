from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_profile import (
    ExplicitProfileModel,
    _drain_profile,
    _fact,
)
from test_profile import (
    profile_context as profile_context,
)
from test_read_api import _headers
from test_read_api import read_api as read_api

import memory_cmic.profile as profile_module
from memory_cmic.models import (
    GovernanceSubjectState,
    MemoryAuditLog,
    MemoryTask,
    ProfileProperty,
)
from memory_cmic.profile import (
    ProfileProposal,
    _apply_config_batch,
    _desired,
    enqueue_profile_rebuild,
    profile_update_status,
    read_profiles,
    run_once,
)
from memory_cmic.profile_config import (
    DEFAULT_DIMENSIONS,
    ProfileConfigUpdate,
    enabled_dimensions,
    ensure_profile_config,
    update_profile_config,
)
from memory_cmic.providers import ProviderError


def _save(session, tenant, dimensions, version=1):
    return update_profile_config(
        session,
        tenant_id=tenant,
        operator_id="admin",
        body=ProfileConfigUpdate(expected_version=version, dimensions=dimensions),
    )


def test_configuration_api_initializes_defaults_and_isolates_tenants(read_api):
    client, _, _, _ = read_api
    assert client.get("/api/v1/profile-config").status_code == 401
    response = client.get("/api/v1/profile-config", headers=_headers())
    assert response.status_code == 200
    original = response.json()
    assert original == {"version": 1, "dimensions": DEFAULT_DIMENSIONS}
    dimensions = deepcopy(original["dimensions"])
    dimensions[0]["name"] = "用户身份"
    dimensions.append({"name": "宠物背景", "description": "用户饲养的宠物及其长期习惯"})
    body = {"expected_version": 1, "dimensions": dimensions}
    assert client.put("/api/v1/profile-config", headers=_headers(), json=body).status_code == 403
    saved = client.put("/api/v1/profile-config", headers=_headers("manager"), json=body)
    assert saved.status_code == 200
    assert saved.json()["version"] == 2
    assert saved.json()["dimensions"][-1]["key"].startswith("dim_")
    assert client.get("/api/v1/profile-config", headers=_headers()).json() == saved.json()
    assert (
        client.get(
            "/api/v1/profile-config",
            headers=_headers("other-tenant"),
        ).json()
        == original
    )
    assert (
        client.put(
            "/api/v1/profile-config",
            headers=_headers("manager"),
            json=body,
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/v1/profile-config",
            headers=_headers("manager"),
            json={**body, "tenant_id": "other"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "dimensions",
    [
        [{"key": "not_owned", "name": "姓名", "description": "姓名"}],
        [{"key": "education", "name": "学历", "description": "学历"}] * 2,
        [{"name": "重复", "description": "描述"}] * 2,
        [{"name": " ", "description": "描述"}],
        [{"name": "名称", "description": " "}],
        [{"name": "名称", "description": "描述", "enabled": "false"}],
        [{"name": f"维度{i}", "description": "描述"} for i in range(33)],
        [{"name": f"维度{i}", "description": "长" * 2048} for i in range(10)],
    ],
)
def test_configuration_rejects_invalid_dimensions_without_partial_writes(read_api, dimensions):
    client, _, _, _ = read_api
    before = client.get("/api/v1/profile-config", headers=_headers()).json()
    response = client.put(
        "/api/v1/profile-config",
        headers=_headers("manager"),
        json={"expected_version": 1, "dimensions": dimensions},
    )
    assert response.status_code == 422
    assert client.get("/api/v1/profile-config", headers=_headers()).json() == before


def test_noop_configuration_save_does_not_rebuild_or_audit(session):
    config = ensure_profile_config(session, "tenant_config_noop")
    saved = _save(session, config.tenant_id, config.dimensions)
    assert saved.version == 1
    assert (
        session.scalar(
            select(MemoryTask.id).where(
                MemoryTask.tenant_id == config.tenant_id,
            )
        )
        is None
    )
    assert (
        session.scalar(
            select(MemoryAuditLog.id).where(
                MemoryAuditLog.tenant_id == config.tenant_id,
            )
        )
        is None
    )


def test_config_change_hides_old_output_then_rebuilds_existing_user(profile_context):
    factory, tenant = profile_context
    model = ExplicitProfileModel()
    with factory.begin() as session:
        _fact(session, tenant, "用户希望邮件回复简洁")
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_profile", force=True)
    _drain_profile(factory, tenant, model)
    with factory.begin() as session:
        old = read_profiles(session, tenant_id=tenant, user_id="user_profile")
        assert len(old) == 1
        old_id = old[0].id
        config = ensure_profile_config(session, tenant)
        dimensions = deepcopy(config.dimensions)
        dimensions[9]["description"] += " 回复要求保留用户条件。"
        _save(session, tenant, dimensions)
        assert read_profiles(session, tenant_id=tenant, user_id="user_profile") == []
        assert profile_update_status(session, tenant, "user_profile") == {
            "config_version": 2,
            "applied_config_version": 1,
            "status": "pending",
        }
        assert (
            session.scalar(
                select(MemoryAuditLog).where(
                    MemoryAuditLog.tenant_id == tenant,
                    MemoryAuditLog.reason_code == "PROFILE_CONFIG_CHANGED",
                )
            ).state_before["version"]
            == 1
        )
    _drain_profile(factory, tenant, model)
    with factory() as session:
        rows = read_profiles(session, tenant_id=tenant, user_id="user_profile")
        assert [row.id for row in rows] == [old_id]
        assert profile_update_status(session, tenant, "user_profile") == {
            "config_version": 2,
            "applied_config_version": 2,
            "status": "ready",
        }


def test_configuration_changed_during_generation_cannot_commit_old_result(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        _fact(session, tenant, "用户希望邮件回复简洁")
        task = enqueue_profile_rebuild(
            session,
            tenant_id=tenant,
            user_id="user_profile",
            force=True,
        )

    class ChangingModel(ExplicitProfileModel):
        def extract(self, memories, *, dimensions):
            with factory.begin() as session:
                _save(session, tenant, [])
            return super().extract(memories, dimensions=dimensions)

    assert run_once(factory, worker_id="config_race", model=ChangingModel())
    with factory() as session:
        assert session.get(MemoryTask, task.id).status == "pending"
        assert (
            session.scalar(
                select(ProfileProperty.id).where(
                    ProfileProperty.tenant_id == tenant,
                )
            )
            is None
        )
    model = ExplicitProfileModel()
    _drain_profile(factory, tenant, model)
    assert model.calls == 0
    with factory() as session:
        assert profile_update_status(session, tenant, "user_profile")["status"] == "ready"
        assert read_profiles(session, tenant_id=tenant, user_id="user_profile") == []


def test_disabling_all_dimensions_invalidates_old_profiles_without_model_calls(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        _fact(session, tenant, "用户希望邮件回复简洁")
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_profile", force=True)
    _drain_profile(factory, tenant, ExplicitProfileModel())
    with factory.begin() as session:
        config = ensure_profile_config(session, tenant)
        _save(session, tenant, [{**d, "enabled": False} for d in config.dimensions])
    model = ExplicitProfileModel()
    _drain_profile(factory, tenant, model)
    assert model.calls == 0
    with factory() as session:
        rows = session.scalars(
            select(ProfileProperty).where(
                ProfileProperty.tenant_id == tenant,
            )
        ).all()
        assert rows and all(row.status == "invalidated" for row in rows)


def test_custom_dimension_preserves_related_person_and_education(profile_context):
    factory, tenant = profile_context
    contents = ["用户的妹妹喜欢画画", "用户于2020年获得本科学位"]
    with factory.begin() as session:
        for content in contents:
            _, memory = _fact(session, tenant, content, business_domains=None)
            if "2020" in content:
                memory.metadata_json = {"temporal_kind": "historical", "retention_kind": "regular"}
        config = _save(
            session,
            tenant,
            [
                {
                    "name": "家庭与教育",
                    "description": "用户学历及重要亲属的长期背景，保留明确主体。",
                }
            ],
        )
        key = config.dimensions[0]["key"]

    class Model:
        def extract(self, memories, *, dimensions):
            assert [d["key"] for d in dimensions] == [key]
            return [
                ProfileProposal(
                    property_key=key,
                    memory_id=m["id"],
                    value=m["content"],
                    quote=m["content"],
                )
                for m in memories
            ]

    _drain_profile(factory, tenant, Model())
    with factory() as session:
        rows = read_profiles(session, tenant_id=tenant, user_id="user_profile")
        assert {row.property_value for row in rows} == set(contents)
        assert {row.property_key for row in rows} == {key}


def test_invalid_or_disabled_dimension_cannot_be_generated(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        _, memory = _fact(session, tenant, "用户希望邮件回复简洁")
        config = ensure_profile_config(session, tenant)
        dimensions = [d for d in enabled_dimensions(config) if d["key"] != "communication"]
        proposal = ProfileProposal(
            property_key="communication",
            memory_id=memory.id,
            value=memory.content,
            quote=memory.content,
        )
        with pytest.raises(ProviderError):
            _desired([memory], [proposal], dimensions)


def test_same_statement_cannot_be_assigned_to_multiple_dimensions(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        _, memory = _fact(session, tenant, "用户喜欢画画")
        config = ensure_profile_config(session, tenant)
        proposals = [
            ProfileProposal(
                property_key=key,
                memory_id=memory.id,
                value=memory.content,
                quote=memory.content,
            )
            for key in ("preferences", "other")
        ]
        with pytest.raises(ProviderError, match="multiple dimensions"):
            _desired([memory], proposals, enabled_dimensions(config))


def test_profile_nonempty_dimension_still_requires_matching_basis_scope(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        _fact(session, tenant, "用户希望邮件回复简洁")
        enqueue_profile_rebuild(session, tenant_id=tenant, user_id="user_profile", force=True)
    _drain_profile(factory, tenant, ExplicitProfileModel())
    with factory.begin() as session:
        row = read_profiles(session, tenant_id=tenant, user_id="user_profile")[0]
        row.business_domains = ["disk"]
        assert read_profiles(session, tenant_id=tenant, user_id="user_profile") == []


def test_config_fanout_persists_cursor_and_includes_users_without_memories(profile_context):
    factory, tenant = profile_context
    with factory.begin() as session:
        session.add_all(
            [GovernanceSubjectState(tenant_id=tenant, user_id=f"u{i:03d}") for i in range(101)]
        )
        _save(session, tenant, [])
        task = session.scalar(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant,
                MemoryTask.task_type == "profile_config_apply",
            )
        )
        task_id = task.id
        assert _apply_config_batch(session, task, False) is False
        assert task.payload["cursor"] == "u099"
    with factory.begin() as session:
        task = session.get(MemoryTask, task_id)
        assert _apply_config_batch(session, task, False) is True
        assert task.payload["cursor"] == "u100"
        jobs = session.scalars(
            select(MemoryTask).where(
                MemoryTask.tenant_id == tenant,
                MemoryTask.task_type == "profile_rebuild",
            )
        ).all()
        assert len(jobs) == 101
        assert len({j.user_id for j in jobs}) == 101


def test_stale_config_fanout_does_not_schedule_old_version(session):
    tenant = "tenant_stale_config"
    session.add(GovernanceSubjectState(tenant_id=tenant, user_id="user_1"))
    _save(session, tenant, [])
    old = session.scalar(select(MemoryTask).where(MemoryTask.tenant_id == tenant))
    _save(session, tenant, [{"name": "背景", "description": "长期背景"}], version=2)
    assert _apply_config_batch(session, old, False) is True
    assert (
        session.scalar(
            select(MemoryTask.id).where(
                MemoryTask.tenant_id == tenant,
                MemoryTask.task_type == "profile_rebuild",
            )
        )
        is None
    )


def test_config_dispatch_cannot_commit_page_after_lease_expiry(profile_context, monkeypatch):
    factory, tenant = profile_context
    with factory.begin() as session:
        _save(session, tenant, [])
        task = session.scalar(select(MemoryTask).where(MemoryTask.tenant_id == tenant))
        task_id = task.id
        task.available_at = datetime.now(UTC) - timedelta(minutes=1)

    def expire_page(session, task, allow_inferred):
        task.payload = {**task.payload, "cursor": "not_committed"}
        task.locked_until = datetime.now(UTC) - timedelta(minutes=1)
        return False

    monkeypatch.setattr(profile_module, "_apply_config_batch", expire_page)
    assert run_once(factory, worker_id="config_lease", model=ExplicitProfileModel())
    with factory() as session:
        task = session.get(MemoryTask, task_id)
        assert task.status == "processing"
        assert "cursor" not in task.payload


def test_failed_rebuild_is_reported_and_does_not_serve_old_config(session):
    tenant = "tenant_config_failed"
    ensure_profile_config(session, tenant)
    session.add(GovernanceSubjectState(tenant_id=tenant, user_id="user_1"))
    session.add(
        MemoryTask(
            id="tsk_config_failed",
            tenant_id=tenant,
            user_id="user_1",
            task_type="profile_rebuild",
            target_type="subject",
            target_id="user_1",
            status="failed",
            idempotency_key="failed",
            completed_at=datetime.now(UTC),
            payload={"config_version": 1},
        )
    )
    assert profile_update_status(session, tenant, "user_1")["status"] == "failed"


def test_configuration_is_not_replaced_by_later_default_template(session, monkeypatch):
    tenant = "tenant_template_version"
    config = ensure_profile_config(session, tenant)
    original = deepcopy(config.dimensions)
    monkeypatch.setattr("memory_cmic.profile_config.DEFAULT_DIMENSIONS", [])
    session.expire_all()
    assert ensure_profile_config(session, tenant).dimensions == original


def test_old_failure_does_not_mark_new_configuration_failed(session):
    tenant = "tenant_config_new_version"
    session.add(GovernanceSubjectState(tenant_id=tenant, user_id="user_1"))
    session.add(
        MemoryTask(
            id="tsk_previous_config_failed",
            tenant_id=tenant,
            user_id="user_1",
            task_type="profile_rebuild",
            target_type="subject",
            target_id="user_1",
            idempotency_key="previous_failed",
            status="failed",
            payload={"config_version": 1},
        )
    )
    _save(session, tenant, [])
    # The tenant task has not scheduled this user's new-version task yet.
    status = profile_update_status(session, tenant, "user_1")
    assert status["status"] == "pending"


def test_config_dispatch_failure_is_reported_for_existing_background(session):
    tenant = "tenant_config_dispatch_failed"
    session.add(
        GovernanceSubjectState(
            tenant_id=tenant,
            user_id="user_1",
            profile_config_version=1,
        )
    )
    _save(session, tenant, [])
    task = session.scalar(
        select(MemoryTask).where(
            MemoryTask.tenant_id == tenant,
            MemoryTask.task_type == "profile_config_apply",
        )
    )
    task.status = "failed"
    # An existing applied version still needs the new configuration, even with zero items.
    assert profile_update_status(session, tenant, "user_1")["status"] == "failed"
