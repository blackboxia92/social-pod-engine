import json
import sqlite3
from uuid import uuid4

import pytest

from social_pod_engine.domain import (
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from social_pod_engine.persistence import SocialPodDatabase


@pytest.fixture
def database(tmp_path):
    database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    database.initialize()
    yield database
    database.close()


def test_persona_and_social_account_round_trip(database):
    persona = Persona(alias="Camila", metadata={"locale": "es-AR"})
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username="camila_x",
        upstream_profile_id="cpm-physical-profile",
        proxy_id="proxy-01",
        group_id="argentina-launch",
        tags=[" Politics ", "politics", "LATAM"],
        editorial_role="Stats and Facts",
    )

    database.save_persona(persona)
    database.save_social_account(account)

    assert database.get_persona(persona.id) == persona
    stored = database.get_social_account(account.id)
    assert stored is not None
    assert stored.persona_id == persona.id
    assert stored.tags == ["politics", "latam"]
    assert stored.editorial_role == "stats-and-facts"
    assert stored.upstream_profile_id == "cpm-physical-profile"


def test_one_persona_can_own_accounts_on_multiple_platforms(database):
    persona = Persona(alias="Sofía")
    database.save_persona(persona)
    for platform in SocialPlatform:
        database.save_social_account(
            SocialAccount(
                persona_id=persona.id,
                platform=platform,
                username=f"sofia_{platform.value}",
            )
        )

    accounts = database.list_social_accounts(persona.id)
    assert {account.platform for account in accounts} == set(SocialPlatform)
    assert {account.persona_id for account in accounts} == {persona.id}


def test_status_dimensions_stay_independent_after_persistence(database):
    persona = Persona(alias="Tomás")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.FACEBOOK,
        username="tomas.fb",
        health_status=HealthStatus.ACTION_REQUIRED,
        session_status=SessionStatus.VALID,
        lifecycle_status=LifecycleStatus.PAUSED,
        quota_status=QuotaStatus.EXHAUSTED,
    )
    database.save_persona(persona)
    database.save_social_account(account)

    restored = database.get_social_account(account.id)
    assert restored is not None
    assert restored.health_status is HealthStatus.ACTION_REQUIRED
    assert restored.session_status is SessionStatus.VALID
    assert restored.lifecycle_status is LifecycleStatus.PAUSED
    assert restored.quota_status is QuotaStatus.EXHAUSTED


def test_tags_use_portable_json_and_group_uuid_type_is_preserved(database):
    persona = Persona(alias="Lucía")
    group_id = uuid4()
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.INSTAGRAM,
        username="lucia.ig",
        group_id=group_id,
        tags=[" Spring ", "spring", "MOBILE"],
        editorial_role="stats_and_facts",
    )
    database.save_persona(persona)
    database.save_social_account(account)

    with sqlite3.connect(database.database_path) as connection:
        stored_tags = connection.execute(
            "SELECT tags FROM social_accounts WHERE id = ?", (str(account.id),)
        ).fetchone()[0]
    assert json.loads(stored_tags) == ["spring", "mobile"]

    restored = database.get_social_account(account.id)
    assert restored is not None
    assert restored.group_id == group_id
    assert restored.editorial_role == "stats_and_facts"


def test_social_database_initialization_does_not_change_an_upstream_database(tmp_path):
    upstream_path = tmp_path / "upstream.sqlite3"
    with sqlite3.connect(upstream_path) as connection:
        connection.execute("CREATE TABLE upstream_profiles (id TEXT PRIMARY KEY)")

    pod_database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    pod_database.initialize()
    pod_database.close()

    with sqlite3.connect(upstream_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"upstream_profiles"}


def test_metadata_rejects_obvious_secret_fields():
    with pytest.raises(ValueError, match="must not contain secrets"):
        Persona(alias="Unsafe", metadata={"api_key": "not-allowed"})
