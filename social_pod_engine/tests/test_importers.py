import json

import pytest
from openpyxl import Workbook

from social_pod_engine.domain import Persona, SocialAccount, SocialPlatform
from social_pod_engine.importers import BulkAccountImporter, ImportRowStatus
from social_pod_engine.importers.normalization import normalize_platform, normalize_username
from social_pod_engine.persistence import SocialPodDatabase


@pytest.fixture
def database(tmp_path):
    database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    database.initialize()
    yield database
    database.close()


def test_platform_and_username_normalization_use_one_identity_key():
    assert {normalize_platform(value) for value in ("X", " x ", "twitter")} == {SocialPlatform.X}
    assert normalize_username(SocialPlatform.X, "  @@ BlackBox IA  ") == "blackboxia"


def test_deduplicates_canonical_identity_inside_a_batch(database):
    result = BulkAccountImporter(database).import_rows(
        [
            {"platform": "twitter", "username": " @One "},
            {"platform": "X", "username": "one"},
        ],
        dry_run=False,
    )

    assert [row.status for row in result.rows] == [ImportRowStatus.CREATED, ImportRowStatus.DUPLICATE]
    assert result.created_accounts == 1
    assert result.skipped_duplicates == 1


def test_csv_reader_normalizes_flexible_headers_and_deduplicates_file_rows(tmp_path, database):
    path = tmp_path / "accounts.csv"
    path.write_text("Platform, UserName\nTwitter, @csv-user\nX,csv-user\n", encoding="utf-8")

    result = BulkAccountImporter(database).import_file(str(path), dry_run=False)

    assert [row.status for row in result.rows] == [ImportRowStatus.CREATED, ImportRowStatus.DUPLICATE]
    assert database.find_account_by_platform_username(SocialPlatform.X, "csv-user") is not None


def test_dry_run_is_strict_and_detects_database_duplicates(database):
    persona = Persona(alias="Existing")
    existing = SocialAccount(persona_id=persona.id, platform=SocialPlatform.X, username="existing")
    database.save_persona(persona)
    database.save_social_account(existing)

    result = BulkAccountImporter(database).import_rows(
        [
            {"platform": "x", "username": "existing"},
            {"platform": "instagram", "username": "new-account", "group_name": "Campaign A"},
        ],
        dry_run=True,
    )

    assert [row.status for row in result.rows] == [ImportRowStatus.DUPLICATE, ImportRowStatus.WOULD_CREATE]
    assert result.created_accounts == result.created_personas == result.created_groups == 0
    assert database.find_account_by_platform_username(SocialPlatform.INSTAGRAM, "new-account") is None
    assert database.find_group_by_alias("Campaign A") is None


def test_real_import_creates_auto_persona_group_and_account(database):
    result = BulkAccountImporter(database).import_rows(
        [
            {
                "platform": "facebook",
                "username": "@Community",
                "group_name": "Luján",
                "tags": " politics, Luján,politics ",
                "editorial_role": "Debate Starter",
            }
        ],
        dry_run=False,
    )

    row = result.rows[0]
    assert row.status is ImportRowStatus.CREATED
    assert (result.created_personas, result.created_groups, result.created_accounts) == (1, 1, 1)
    persona = database.get_persona(row.persona_id)
    account = database.get_social_account(row.account_id)
    assert persona is not None and persona.alias == "auto:facebook:community"
    assert persona.metadata["is_auto_generated"] is True
    assert account is not None
    assert account.tags == ["politics", "luján"]
    assert account.editorial_role == "debate-starter"


def test_metadata_is_separated_by_scope_and_rejects_explicit_field_duplicates(database):
    importer = BulkAccountImporter(database)
    result = importer.import_rows(
        [
            {
                "platform": "x",
                "username": "scope-test",
                "alias_persona": "Scope Persona",
                "metadata": json.dumps({"locale": "es-AR"}),
                "account_metadata": json.dumps({"channel_tone": "formal"}),
                "proxy_id": "proxy-1",
            },
            {
                "platform": "x",
                "username": "invalid-metadata",
                "metadata": json.dumps({"proxy_id": "must-not-be-here"}),
            },
        ],
        dry_run=False,
    )

    assert [row.status for row in result.rows] == [ImportRowStatus.CREATED, ImportRowStatus.INVALID]
    persona = database.find_persona_by_alias("Scope Persona")
    account = database.find_account_by_platform_username(SocialPlatform.X, "scope-test")
    assert persona is not None and persona.metadata == {"locale": "es-AR"}
    assert account is not None
    assert account.metadata == {"channel_tone": "formal"}
    assert account.proxy_id == "proxy-1"


def test_invalid_rows_do_not_abort_following_valid_rows(database):
    result = BulkAccountImporter(database).import_rows(
        [
            {"platform": "unsupported", "username": "bad"},
            {"platform": "instagram", "username": "good"},
        ],
        dry_run=False,
    )

    assert [row.status for row in result.rows] == [ImportRowStatus.INVALID, ImportRowStatus.CREATED]
    assert result.invalid_rows == 1
    assert result.created_accounts == 1


def test_xlsx_uses_first_visible_sheet_by_default(tmp_path, database):
    workbook = Workbook()
    hidden = workbook.active
    hidden.title = "hidden"
    hidden.append(["platform", "username"])
    hidden.append(["x", "hidden-account"])
    visible = workbook.create_sheet("accounts")
    visible.append([" Platform ", "USERNAME", "Alias Persona"])
    visible.append(["Twitter", "@spreadsheet", "Spreadsheet Persona"])
    hidden.sheet_state = "hidden"
    path = tmp_path / "accounts.xlsx"
    workbook.save(path)
    workbook.close()

    result = BulkAccountImporter(database).import_file(str(path), dry_run=False)

    assert result.total_rows == 1
    assert result.rows[0].status is ImportRowStatus.CREATED
    assert database.find_account_by_platform_username(SocialPlatform.X, "spreadsheet") is not None
