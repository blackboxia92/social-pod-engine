"""Batch account staging with explicit dry-run and row-level error handling."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..domain import AccountGroup, Persona, SocialAccount, normalize_metadata
from ..persistence import SocialPodDatabase
from .models import ImportResult, ImportRowResult, ImportRowStatus
from .normalization import account_identity_key, normalize_platform, normalize_username
from .readers import read_import_file

_FORBIDDEN_METADATA_FIELDS = {
    "proxy_id",
    "group_id",
    "editorial_role",
    "lifecycle_status",
    "session_status",
    "health_status",
    "quota_status",
}


class BulkAccountImporter:
    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def import_file(
        self, path: str, *, dry_run: bool = True, sheet_name: str | None = None
    ) -> ImportResult:
        return self.import_rows(read_import_file(path, sheet_name=sheet_name), dry_run=dry_run)

    def import_rows(
        self, rows: Iterable[Mapping[str, Any]], *, dry_run: bool = True
    ) -> ImportResult:
        result = ImportResult()
        batch_keys: set[tuple[object, str]] = set()
        planned_personas: dict[str, Persona] = {}
        planned_groups: dict[str, AccountGroup] = {}

        for row_number, raw_row in enumerate(rows, start=2):
            self._import_row(
                raw_row,
                row_number=row_number,
                dry_run=dry_run,
                batch_keys=batch_keys,
                planned_personas=planned_personas,
                planned_groups=planned_groups,
                result=result,
            )
        return result

    def _import_row(
        self,
        raw_row: Mapping[str, Any],
        *,
        row_number: int,
        dry_run: bool,
        batch_keys: set[tuple[object, str]],
        planned_personas: dict[str, Persona],
        planned_groups: dict[str, AccountGroup],
        result: ImportResult,
    ) -> None:
        platform = None
        username = None
        try:
            platform = normalize_platform(_required_text(raw_row, "platform"))
            username = normalize_username(platform, _required_text(raw_row, "username"))
            identity_key = account_identity_key(platform, username)
            if identity_key in batch_keys:
                result.add_row(
                    ImportRowResult(row_number, platform, username, ImportRowStatus.DUPLICATE, reason="duplicate in batch")
                )
                return
            if self.database.find_account_by_platform_username(platform, username) is not None:
                result.add_row(
                    ImportRowResult(row_number, platform, username, ImportRowStatus.DUPLICATE, reason="already exists")
                )
                return

            persona, persona_is_new = self._resolve_persona(raw_row, platform, username, planned_personas)
            group, group_is_new = self._resolve_group(raw_row, planned_groups)
            account = SocialAccount(
                persona_id=persona.id,
                platform=platform,
                username=username,
                upstream_profile_id=_optional_text(raw_row.get("upstream_profile_id")),
                proxy_id=_optional_text(raw_row.get("proxy_id")),
                group_id=group.id if group else None,
                tags=_parse_tags(raw_row.get("tags")),
                editorial_role=_optional_text(raw_row.get("editorial_role")),
                metadata=_parse_metadata(raw_row.get("account_metadata"), field_name="account_metadata"),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            result.add_row(
                ImportRowResult(row_number, platform, username, ImportRowStatus.INVALID, reason=str(exc))
            )
            return

        batch_keys.add(identity_key)
        if dry_run:
            result.add_row(
                ImportRowResult(
                    row_number,
                    platform,
                    username,
                    ImportRowStatus.WOULD_CREATE,
                    persona_id=persona.id,
                    group_id=group.id if group else None,
                    account_id=account.id,
                )
            )
            return

        if persona_is_new:
            self.database.save_persona(persona)
            result.created_personas += 1
        if group is not None and group_is_new:
            self.database.save_account_group(group)
            result.created_groups += 1
        self.database.save_social_account(account)
        result.created_accounts += 1
        result.add_row(
            ImportRowResult(
                row_number,
                platform,
                username,
                ImportRowStatus.CREATED,
                persona_id=persona.id,
                group_id=group.id if group else None,
                account_id=account.id,
            )
        )

    def _resolve_persona(self, row, platform, username, planned_personas):
        alias = _optional_text(row.get("alias_persona")) or f"auto:{platform.value}:{username}"
        existing = planned_personas.get(alias) or self.database.find_persona_by_alias(alias)
        if existing is not None:
            return existing, False
        metadata = _parse_metadata(row.get("metadata"), field_name="metadata")
        if row.get("alias_persona") is None or not _optional_text(row.get("alias_persona")):
            metadata = {**metadata, "is_auto_generated": True}
        persona = Persona(alias=alias, metadata=metadata)
        planned_personas[alias] = persona
        return persona, True

    def _resolve_group(self, row, planned_groups):
        alias = _optional_text(row.get("group_name"))
        if alias is None:
            return None, False
        existing = planned_groups.get(alias) or self.database.find_group_by_alias(alias)
        if existing is not None:
            return existing, False
        group = AccountGroup(alias=alias)
        planned_groups[alias] = group
        return group, True


def _required_text(row: Mapping[str, Any], name: str) -> str:
    value = _optional_text(row.get(name))
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value.strip() or None


def _parse_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        raise TypeError("tags must be a comma-separated string")
    return value.split(",")


def _parse_metadata(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        metadata = dict(value)
    elif isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError(f"{field_name} must be a JSON object")
        metadata = parsed
    else:
        raise TypeError(f"{field_name} must be a JSON object or JSON string")
    duplicate_fields = _FORBIDDEN_METADATA_FIELDS.intersection(metadata)
    if duplicate_fields:
        names = ", ".join(sorted(duplicate_fields))
        raise ValueError(f"{field_name} must not duplicate explicit fields: {names}")
    return normalize_metadata(metadata)
