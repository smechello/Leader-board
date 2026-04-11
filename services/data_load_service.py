from __future__ import annotations

import json
import re
import secrets
import string
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from models import db
from models.options import ProcessOption, SystemSetting, ThemeOption
from models.scoring import ScoringCategorySetting
from models.score import SCORE_CATEGORIES
from models.team import Project, Team, TeamMember
from models.user import Judge, User
from services.scoring_config_service import DEFAULT_SCORING_RULES, recalculate_all_weighted_scores

PRESENTATION_TIME_LIMIT_KEY = "presentation_time_limit_seconds"
DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS = 300
USERNAME_PATTERN = re.compile(r"[a-z0-9_.-]{3,80}")


class DataLoadValidationError(ValueError):
    """Raised when load-data JSON cannot be parsed or validated."""


def _normalize_name_token(raw_value: Any) -> str:
    value = str(raw_value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return normalized or "user"


def _make_internal_email(name_token: str) -> str:
    return f"{name_token}_{uuid.uuid4().hex[:10]}@internal.local"


def _generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(max(10, length)))


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DataLoadValidationError(f"{field_name} must be a valid number.") from exc
    return parsed


def _validate_optional_url(label: str, value: Any) -> str | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None

    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DataLoadValidationError(f"{label} must start with http:// or https://")

    return cleaned


def _extract_named_items(raw_items: Any, label: str) -> list[str]:
    if raw_items in (None, ""):
        return []
    if not isinstance(raw_items, list):
        raise DataLoadValidationError(f"{label} must be a list.")

    values: list[str] = []
    for index, item in enumerate(raw_items, start=1):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()

        if not name:
            raise DataLoadValidationError(f"{label} entry #{index} is missing a name.")
        values.append(name)

    return values


def _dedupe_casefold(values: list[str]) -> list[str]:
    seen = set()
    unique_values: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_values.append(value)
    return unique_values


def _normalize_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text_value = str(value).strip().lower()
    if text_value in {"1", "true", "yes", "y", "on"}:
        return True
    if text_value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_scoring_rules(raw_payload: dict[str, Any]) -> dict[str, dict[str, float]] | None:
    scoring_rules = raw_payload.get("scoring_rules")
    if scoring_rules in (None, ""):
        return None

    if not isinstance(scoring_rules, dict):
        raise DataLoadValidationError("scoring_rules must be an object keyed by category.")

    normalized: dict[str, dict[str, float]] = {}
    total_weight = Decimal("0")

    for category in SCORE_CATEGORIES:
        entry = scoring_rules.get(category)
        if not isinstance(entry, dict):
            raise DataLoadValidationError(f"scoring_rules.{category} is required.")

        weight = _to_decimal(entry.get("weight_percent"), f"scoring_rules.{category}.weight_percent")
        max_score = _to_decimal(entry.get("max_score"), f"scoring_rules.{category}.max_score")

        if weight <= 0:
            raise DataLoadValidationError(f"scoring_rules.{category}.weight_percent must be greater than 0.")
        if max_score <= 0:
            raise DataLoadValidationError(f"scoring_rules.{category}.max_score must be greater than 0.")

        weight = weight.quantize(Decimal("0.01"))
        max_score = max_score.quantize(Decimal("0.01"))
        total_weight += weight

        normalized[category] = {
            "weight_percent": float(weight),
            "max_score": float(max_score),
        }

    if total_weight.quantize(Decimal("0.01")) != Decimal("100.00"):
        raise DataLoadValidationError("scoring_rules weights must total exactly 100.")

    return normalized


def _parse_presentation_settings(raw_payload: dict[str, Any]) -> dict[str, int] | None:
    presentation_settings = raw_payload.get("presentation_settings")
    if presentation_settings in (None, ""):
        return None
    if not isinstance(presentation_settings, dict):
        raise DataLoadValidationError("presentation_settings must be an object.")

    raw_minutes = presentation_settings.get("time_limit_minutes")
    if raw_minutes in (None, ""):
        return None

    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError) as exc:
        raise DataLoadValidationError("presentation_settings.time_limit_minutes must be a whole number.") from exc

    if minutes < 1 or minutes > 60:
        raise DataLoadValidationError("presentation_settings.time_limit_minutes must be between 1 and 60.")

    return {"time_limit_minutes": minutes}


def parse_json_payload(raw_json_text: str) -> dict[str, Any]:
    text_value = (raw_json_text or "").strip()
    if not text_value:
        raise DataLoadValidationError("Provide JSON data by uploading a file or pasting JSON text.")

    try:
        payload = json.loads(text_value)
    except json.JSONDecodeError as exc:
        raise DataLoadValidationError(f"Invalid JSON: {exc.msg} (line {exc.lineno})") from exc

    if not isinstance(payload, dict):
        raise DataLoadValidationError("Root JSON must be an object.")

    return payload


def build_load_data_template() -> dict[str, Any]:
    return {
        "meta": {
            "template_version": "1.0",
            "description": "Bulk admin data loader template",
        },
        "processes": [
            "General",
            "Healthcare",
            "Education",
        ],
        "themes": [
            "AI",
            "Automation",
            "Sustainability",
        ],
        "presentation_settings": {
            "time_limit_minutes": 5,
        },
        "scoring_rules": {
            category: {
                "weight_percent": float(DEFAULT_SCORING_RULES[category]["weight_percent"]),
                "max_score": float(DEFAULT_SCORING_RULES[category]["max_score"]),
            }
            for category in SCORE_CATEGORIES
        },
        "teams": [
            {
                "team_name": "Team Alpha",
                "process": "General",
                "theme": "AI",
                "project": {
                    "project_title": "Intelligent Ops Assistant",
                    "problem_statement": "Slow manual operations reduce productivity.",
                    "project_summary": "A workflow assistant that reduces turnaround time using automation and AI.",
                    "repository_url": "https://example.com/repo",
                    "demo_url": "https://example.com/demo",
                    "notes_url": "https://example.com/notes",
                },
                "portal_access": {
                    "login_id": "team_alpha",
                    "password": "",  
                },
                "members": [
                    {
                        "full_name": "Alice Johnson",
                        "email": "alice@example.com",
                        "phone": "",
                        "department_or_class": "CSE",
                    },
                    {
                        "full_name": "Bob Smith",
                        "email": "",
                        "phone": "",
                        "department_or_class": "ECE",
                    },
                ],
            }
        ],
        "judges": [
            {
                "display_name": "Dr. Priya Rao",
                "username": "",  
                "password": "",  
                "organization": "CorpTech",
                "phone": "",
                "is_active": True,
            }
        ],
    }


def _get_append_state() -> dict[str, Any]:
    process_names = [row.name for row in ProcessOption.query.all()]
    theme_names = [row.name for row in ThemeOption.query.all()]
    teams = Team.query.all()
    users = User.query.all()

    return {
        "processes": {name.casefold() for name in process_names},
        "themes": {name.casefold() for name in theme_names},
        "teams_by_name": {team.team_name.casefold(): team for team in teams},
        "users_by_username": {user.username.casefold(): user for user in users},
        "team_logins": {
            (team.portal_login_id or "").casefold(): team.id
            for team in teams
            if team.portal_login_id
        },
    }


def _ensure_unique_username(base: str, taken_usernames: set[str]) -> str:
    candidate = base
    suffix = 1
    while candidate.casefold() in taken_usernames:
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def _ensure_unique_team_login(base: str, taken_logins: set[str]) -> str:
    if len(base) < 3:
        base = f"{base}_team"
    candidate = base[:80]
    suffix = 1
    while candidate.casefold() in taken_logins:
        suffix += 1
        candidate = f"{base[:72]}_{suffix}"[:80]
    return candidate


def prepare_load_payload(raw_payload: dict[str, Any], mode: str = "append") -> tuple[dict[str, Any], dict[str, Any]]:
    selected_mode = (mode or "append").strip().lower()
    if selected_mode not in {"append", "clear_load"}:
        raise DataLoadValidationError("import mode must be append or clear_load.")

    append_state = _get_append_state() if selected_mode == "append" else {
        "processes": set(),
        "themes": set(),
        "teams_by_name": {},
        "users_by_username": {},
        "team_logins": {},
    }

    process_values = _extract_named_items(raw_payload.get("processes"), "processes")
    theme_values = _extract_named_items(raw_payload.get("themes"), "themes")

    if "teams" not in raw_payload or not isinstance(raw_payload.get("teams"), list):
        raise DataLoadValidationError("teams must be a list and is required.")

    if "judges" not in raw_payload or not isinstance(raw_payload.get("judges"), list):
        raise DataLoadValidationError("judges must be a list and is required.")

    teams_input: list[dict[str, Any]] = raw_payload.get("teams", [])
    judges_input: list[dict[str, Any]] = raw_payload.get("judges", [])

    normalized_teams: list[dict[str, Any]] = []
    normalized_judges: list[dict[str, Any]] = []
    generated_judge_credentials: list[dict[str, Any]] = []
    generated_portal_credentials: list[dict[str, Any]] = []

    used_team_names_in_payload: set[str] = set()
    used_usernames = set(append_state["users_by_username"].keys())
    used_team_logins = set(append_state["team_logins"].keys())

    process_values_from_teams: list[str] = []
    theme_values_from_teams: list[str] = []

    member_total = 0
    team_create_count = 0
    team_update_count = 0

    for team_index, team_item in enumerate(teams_input, start=1):
        if not isinstance(team_item, dict):
            raise DataLoadValidationError(f"teams[{team_index}] must be an object.")

        team_name = str(team_item.get("team_name") or "").strip()
        if not team_name:
            raise DataLoadValidationError(f"teams[{team_index}].team_name is required.")

        team_key = team_name.casefold()
        if team_key in used_team_names_in_payload:
            raise DataLoadValidationError(f"Duplicate team_name found in payload: {team_name}")
        used_team_names_in_payload.add(team_key)

        process_name = str(team_item.get("process") or "General").strip() or "General"
        theme_name = str(team_item.get("theme") or "General").strip() or "General"

        process_values_from_teams.append(process_name)
        theme_values_from_teams.append(theme_name)

        project = team_item.get("project")
        if not isinstance(project, dict):
            raise DataLoadValidationError(f"teams[{team_index}].project is required and must be an object.")

        project_title = str(project.get("project_title") or "").strip()
        problem_statement = str(project.get("problem_statement") or "").strip()
        project_summary = str(project.get("project_summary") or "").strip()

        if not project_title or not problem_statement or not project_summary:
            raise DataLoadValidationError(
                f"teams[{team_index}] project_title, problem_statement, and project_summary are required."
            )

        repository_url = _validate_optional_url("repository_url", project.get("repository_url"))
        demo_url = _validate_optional_url("demo_url", project.get("demo_url"))
        notes_url = _validate_optional_url("notes_url", project.get("notes_url"))

        existing_team = append_state["teams_by_name"].get(team_key)
        action = "update" if existing_team else "create"
        if action == "create":
            team_create_count += 1
        else:
            team_update_count += 1

        portal_access_raw = team_item.get("portal_access") if isinstance(team_item.get("portal_access"), dict) else {}
        login_id = str(
            portal_access_raw.get("login_id")
            or team_item.get("portal_login_id")
            or ""
        ).strip()
        login_password = str(portal_access_raw.get("password") or "").strip()

        if login_password and not login_id:
            generated_login_id = _ensure_unique_team_login(_normalize_name_token(team_name), used_team_logins)
            login_id = generated_login_id

        generated_portal_password = False
        if login_id:
            if len(login_id) < 3:
                raise DataLoadValidationError(
                    f"teams[{team_index}].portal_access.login_id must be at least 3 characters."
                )

            login_key = login_id.casefold()
            owner_team_id = append_state["team_logins"].get(login_key)
            if owner_team_id and (not existing_team or owner_team_id != existing_team.id):
                raise DataLoadValidationError(
                    f"teams[{team_index}] portal login ID '{login_id}' is already used by another team."
                )

            if login_key in used_team_logins and (not existing_team or (existing_team.portal_login_id or "").casefold() != login_key):
                raise DataLoadValidationError(
                    f"Duplicate portal login ID in payload: {login_id}"
                )
            used_team_logins.add(login_key)

            if not login_password:
                has_existing_same_login = bool(
                    existing_team
                    and existing_team.portal_login_id
                    and existing_team.portal_login_id.casefold() == login_key
                    and existing_team.portal_password_hash
                )
                if selected_mode == "clear_load" or not has_existing_same_login:
                    login_password = _generate_password()
                    generated_portal_password = True

            if login_password and len(login_password) < 8:
                raise DataLoadValidationError(
                    f"teams[{team_index}] portal password must be at least 8 characters."
                )

            if generated_portal_password:
                generated_portal_credentials.append(
                    {
                        "team_name": team_name,
                        "login_id": login_id,
                        "password": login_password,
                    }
                )

        members_input = team_item.get("members") or []
        if not isinstance(members_input, list):
            raise DataLoadValidationError(f"teams[{team_index}].members must be a list.")

        normalized_members: list[dict[str, str | None]] = []
        member_emails: set[str] = set()
        for member_index, member_item in enumerate(members_input, start=1):
            if not isinstance(member_item, dict):
                raise DataLoadValidationError(
                    f"teams[{team_index}].members[{member_index}] must be an object."
                )

            full_name = str(member_item.get("full_name") or "").strip()
            if not full_name:
                raise DataLoadValidationError(
                    f"teams[{team_index}].members[{member_index}].full_name is required."
                )

            email_value = str(member_item.get("email") or "").strip().lower()
            if not email_value:
                email_value = _make_internal_email(_normalize_name_token(f"{team_name}_{full_name}"))

            email_key = email_value.casefold()
            if email_key in member_emails:
                raise DataLoadValidationError(
                    f"Duplicate member email in team '{team_name}': {email_value}"
                )
            member_emails.add(email_key)

            normalized_members.append(
                {
                    "full_name": full_name,
                    "email": email_value,
                    "phone": str(member_item.get("phone") or "").strip() or None,
                    "department_or_class": str(member_item.get("department_or_class") or "").strip() or None,
                }
            )

        member_total += len(normalized_members)

        normalized_teams.append(
            {
                "team_name": team_name,
                "process": process_name,
                "theme": theme_name,
                "is_active": _normalize_bool(team_item.get("is_active"), default=True),
                "project": {
                    "project_title": project_title,
                    "problem_statement": problem_statement,
                    "project_summary": project_summary,
                    "repository_url": repository_url,
                    "demo_url": demo_url,
                    "notes_url": notes_url,
                },
                "portal_access": {
                    "login_id": login_id or None,
                    "password": login_password or None,
                },
                "members": normalized_members,
                "action": action,
            }
        )

    judge_create_count = 0
    judge_update_count = 0

    for judge_index, judge_item in enumerate(judges_input, start=1):
        if not isinstance(judge_item, dict):
            raise DataLoadValidationError(f"judges[{judge_index}] must be an object.")

        display_name = str(judge_item.get("display_name") or "").strip()
        if not display_name:
            raise DataLoadValidationError(f"judges[{judge_index}].display_name is required.")

        provided_username = str(judge_item.get("username") or "").strip().lower()
        generated_username = False
        if not provided_username:
            base_username = _normalize_name_token(display_name)
            if len(base_username) < 3:
                base_username = f"{base_username}_jd"
            provided_username = _ensure_unique_username(base_username[:80], used_usernames)
            generated_username = True

        if not USERNAME_PATTERN.fullmatch(provided_username):
            raise DataLoadValidationError(
                f"judges[{judge_index}].username must match [a-z0-9_.-] and be 3-80 characters."
            )

        username_key = provided_username.casefold()
        existing_user = append_state["users_by_username"].get(username_key)
        if existing_user and existing_user.role != "judge":
            raise DataLoadValidationError(
                f"judges[{judge_index}].username '{provided_username}' belongs to a non-judge account."
            )

        if username_key in used_usernames and not existing_user:
            raise DataLoadValidationError(f"Duplicate judge username in payload: {provided_username}")
        used_usernames.add(username_key)

        judge_password = str(judge_item.get("password") or "").strip()
        generated_password = False
        is_new_judge = existing_user is None
        if not judge_password and is_new_judge:
            judge_password = _generate_password()
            generated_password = True

        if judge_password and len(judge_password) < 8:
            raise DataLoadValidationError(f"judges[{judge_index}].password must be at least 8 characters.")

        action = "create" if is_new_judge else "update"
        if action == "create":
            judge_create_count += 1
        else:
            judge_update_count += 1

        if generated_username or generated_password:
            generated_judge_credentials.append(
                {
                    "display_name": display_name,
                    "username": provided_username,
                    "password": judge_password,
                    "generated_username": generated_username,
                    "generated_password": generated_password,
                    "action": action,
                }
            )

        normalized_judges.append(
            {
                "display_name": display_name,
                "username": provided_username,
                "password": judge_password or None,
                "phone": str(judge_item.get("phone") or "").strip() or None,
                "organization": str(judge_item.get("organization") or "").strip() or None,
                "is_active": _normalize_bool(judge_item.get("is_active"), default=True),
                "action": action,
            }
        )

    all_processes = _dedupe_casefold(process_values + process_values_from_teams)
    all_themes = _dedupe_casefold(theme_values + theme_values_from_teams)

    if not all_processes:
        all_processes = ["General"]
    if not all_themes:
        all_themes = ["General"]

    scoring_rules = _parse_scoring_rules(raw_payload)
    presentation_settings = _parse_presentation_settings(raw_payload)

    existing_processes = append_state["processes"]
    existing_themes = append_state["themes"]

    operations = {
        "processes_new": sum(1 for name in all_processes if name.casefold() not in existing_processes),
        "processes_existing": sum(1 for name in all_processes if name.casefold() in existing_processes),
        "themes_new": sum(1 for name in all_themes if name.casefold() not in existing_themes),
        "themes_existing": sum(1 for name in all_themes if name.casefold() in existing_themes),
        "teams_create": team_create_count,
        "teams_update": team_update_count,
        "judges_create": judge_create_count,
        "judges_update": judge_update_count,
    }

    normalized_payload = {
        "meta": raw_payload.get("meta") if isinstance(raw_payload.get("meta"), dict) else {},
        "processes": all_processes,
        "themes": all_themes,
        "presentation_settings": presentation_settings or {},
        "scoring_rules": scoring_rules or {},
        "teams": normalized_teams,
        "judges": normalized_judges,
    }

    preview_summary = {
        "mode": selected_mode,
        "counts": {
            "processes": len(all_processes),
            "themes": len(all_themes),
            "teams": len(normalized_teams),
            "members": member_total,
            "judges": len(normalized_judges),
        },
        "operations": operations,
        "teams": [
            {
                "team_name": team["team_name"],
                "process": team["process"],
                "theme": team["theme"],
                "project_title": team["project"]["project_title"],
                "members": len(team["members"]),
                "action": team["action"],
            }
            for team in normalized_teams
        ],
        "judges": [
            {
                "display_name": judge["display_name"],
                "username": judge["username"],
                "action": judge["action"],
                "has_password": bool(judge["password"]),
            }
            for judge in normalized_judges
        ],
        "generated_judge_credentials": generated_judge_credentials,
        "generated_portal_credentials": generated_portal_credentials,
        "has_custom_scoring": bool(scoring_rules),
        "has_custom_presentation_limit": bool(presentation_settings),
    }

    return normalized_payload, preview_summary


def _truncate_all_tables() -> None:
    table_names = [table.name for table in db.metadata.sorted_tables if table.name]
    if not table_names:
        return
    quoted_table_list = ", ".join(f'"{name}"' for name in table_names)
    db.session.execute(text(f"TRUNCATE TABLE {quoted_table_list} RESTART IDENTITY CASCADE"))


def _set_system_setting(key: str, value: str) -> None:
    row = SystemSetting.query.filter_by(key=key).first()
    if row is None:
        row = SystemSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


def _ensure_scoring_defaults_if_empty() -> None:
    existing_count = ScoringCategorySetting.query.count()
    if existing_count > 0:
        return

    for category_key in SCORE_CATEGORIES:
        defaults = DEFAULT_SCORING_RULES[category_key]
        db.session.add(
            ScoringCategorySetting(
                category=category_key,
                weight_percent=defaults["weight_percent"],
                max_score=defaults["max_score"],
            )
        )

    db.session.flush()
    recalculate_all_weighted_scores(rules_map=DEFAULT_SCORING_RULES)


def _apply_scoring_rules(scoring_rules: dict[str, dict[str, float]]) -> None:
    updates: dict[str, dict[str, Decimal]] = {}

    for category_key in SCORE_CATEGORIES:
        values = scoring_rules[category_key]
        updates[category_key] = {
            "weight_percent": _to_decimal(values["weight_percent"], f"{category_key}.weight_percent").quantize(Decimal("0.01")),
            "max_score": _to_decimal(values["max_score"], f"{category_key}.max_score").quantize(Decimal("0.01")),
        }

    for category_key in SCORE_CATEGORIES:
        row = ScoringCategorySetting.query.filter_by(category=category_key).first()
        if row is None:
            row = ScoringCategorySetting(category=category_key)
            db.session.add(row)

        row.weight_percent = updates[category_key]["weight_percent"]
        row.max_score = updates[category_key]["max_score"]

    db.session.flush()
    recalculate_all_weighted_scores(rules_map=updates)


def apply_load_payload(prepared_payload: dict[str, Any], mode: str = "append") -> dict[str, Any]:
    selected_mode = (mode or "append").strip().lower()
    if selected_mode not in {"append", "clear_load"}:
        raise DataLoadValidationError("import mode must be append or clear_load.")

    if selected_mode == "clear_load":
        _truncate_all_tables()

    summary = {
        "mode": selected_mode,
        "processes_created": 0,
        "themes_created": 0,
        "teams_created": 0,
        "teams_updated": 0,
        "members_created": 0,
        "members_updated": 0,
        "judges_created": 0,
        "judges_updated": 0,
        "scoring_rules_updated": 0,
        "presentation_limit_updated": False,
    }

    existing_processes = {row.name.casefold(): row for row in ProcessOption.query.all()}
    for process_name in prepared_payload.get("processes", []):
        key = process_name.casefold()
        if key in existing_processes:
            continue
        row = ProcessOption(name=process_name)
        db.session.add(row)
        existing_processes[key] = row
        summary["processes_created"] += 1

    existing_themes = {row.name.casefold(): row for row in ThemeOption.query.all()}
    for theme_name in prepared_payload.get("themes", []):
        key = theme_name.casefold()
        if key in existing_themes:
            continue
        row = ThemeOption(name=theme_name)
        db.session.add(row)
        existing_themes[key] = row
        summary["themes_created"] += 1

    max_sort_order = db.session.query(db.func.coalesce(db.func.max(Team.sort_order), 0)).scalar() or 0
    existing_teams = {team.team_name.casefold(): team for team in Team.query.all()}

    for team_data in prepared_payload.get("teams", []):
        team_name = team_data["team_name"]
        team_key = team_name.casefold()
        team = existing_teams.get(team_key)

        if team is None:
            max_sort_order += 1
            team = Team(team_name=team_name, sort_order=max_sort_order)
            db.session.add(team)
            existing_teams[team_key] = team
            summary["teams_created"] += 1
        else:
            summary["teams_updated"] += 1

        team.team_name = team_name
        team.process = team_data["process"]
        team.theme = team_data["theme"]
        team.is_active = bool(team_data.get("is_active", True))

        project_data = team_data["project"]
        project = team.project
        if project is None:
            project = Project(team=team)
            db.session.add(project)

        project.project_title = project_data["project_title"]
        project.problem_statement = project_data["problem_statement"]
        project.project_summary = project_data["project_summary"]
        project.repository_url = project_data.get("repository_url")
        project.demo_url = project_data.get("demo_url")
        project.notes_url = project_data.get("notes_url")

        portal_access = team_data.get("portal_access") or {}
        login_id = portal_access.get("login_id")
        portal_password = portal_access.get("password")

        if login_id:
            conflict = Team.query.filter(Team.portal_login_id == login_id, Team.id != team.id).first()
            if conflict is not None:
                raise DataLoadValidationError(f"Team portal login ID '{login_id}' is already used by another team.")
            team.portal_login_id = login_id
            if portal_password:
                team.portal_password_hash = generate_password_hash(portal_password)

        existing_members_by_email = {
            (member.email or "").casefold(): member
            for member in team.members
            if member.email
        }

        for member_data in team_data.get("members", []):
            member_key = (member_data["email"] or "").casefold()
            member = existing_members_by_email.get(member_key)
            if member is None:
                member = TeamMember(team=team)
                db.session.add(member)
                existing_members_by_email[member_key] = member
                summary["members_created"] += 1
            else:
                summary["members_updated"] += 1

            member.full_name = member_data["full_name"]
            member.email = member_data["email"]
            member.phone = member_data.get("phone")
            member.department_or_class = member_data.get("department_or_class")

    existing_users = {user.username.casefold(): user for user in User.query.all()}

    for judge_data in prepared_payload.get("judges", []):
        username = judge_data["username"]
        username_key = username.casefold()
        user = existing_users.get(username_key)

        if user is None:
            password_value = judge_data.get("password") or _generate_password()
            user = User(
                username=username,
                email=_make_internal_email(_normalize_name_token(username)),
                password_hash=generate_password_hash(password_value),
                role="judge",
                is_active=bool(judge_data.get("is_active", True)),
            )
            judge_profile = Judge(
                user=user,
                display_name=judge_data["display_name"],
                phone=judge_data.get("phone"),
                organization=judge_data.get("organization"),
                is_active=bool(judge_data.get("is_active", True)),
            )
            db.session.add(user)
            db.session.add(judge_profile)
            existing_users[username_key] = user
            summary["judges_created"] += 1
            continue

        if user.role != "judge":
            raise DataLoadValidationError(f"Username '{username}' is not a judge account.")

        user.is_active = bool(judge_data.get("is_active", True))
        if judge_data.get("password"):
            user.password_hash = generate_password_hash(judge_data["password"])

        judge_profile = user.judge_profile
        if judge_profile is None:
            judge_profile = Judge(user=user, display_name=judge_data["display_name"])
            db.session.add(judge_profile)

        judge_profile.display_name = judge_data["display_name"]
        judge_profile.phone = judge_data.get("phone")
        judge_profile.organization = judge_data.get("organization")
        judge_profile.is_active = bool(judge_data.get("is_active", True))
        summary["judges_updated"] += 1

    scoring_rules = prepared_payload.get("scoring_rules") or {}
    if scoring_rules:
        _apply_scoring_rules(scoring_rules)
        summary["scoring_rules_updated"] = len(SCORE_CATEGORIES)
    elif selected_mode == "clear_load":
        _ensure_scoring_defaults_if_empty()

    presentation_settings = prepared_payload.get("presentation_settings") or {}
    minutes = presentation_settings.get("time_limit_minutes")
    if minutes is not None:
        _set_system_setting(PRESENTATION_TIME_LIMIT_KEY, str(int(minutes) * 60))
        summary["presentation_limit_updated"] = True
    elif selected_mode == "clear_load":
        _set_system_setting(PRESENTATION_TIME_LIMIT_KEY, str(DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS))
        summary["presentation_limit_updated"] = True

    return summary
