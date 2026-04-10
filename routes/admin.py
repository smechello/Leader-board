from datetime import datetime, timedelta, timezone
import json
import re
import secrets
import uuid
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash

from models import db
from models.audit import AuditLog
from models.auth_access import (
    LOGIN_REQUEST_STATUS_APPROVED,
    LOGIN_REQUEST_STATUS_EXPIRED,
    LOGIN_REQUEST_STATUS_PENDING,
    LOGIN_REQUEST_STATUS_REJECTED,
    JudgeDirectLoginLink,
    JudgeLoginRequest,
    TeamDirectLoginLink,
)
from models.options import ProcessOption, SystemSetting, ThemeOption
from models.score import Score
from models.scoring import ScoringCategorySetting
from models.team import Project, Team, TeamMember
from models.user import Judge, User
from services.presence_service import get_judge_online_map
from services.scoring_config_service import (
    DEFAULT_SCORING_RULES,
    get_category_definitions,
    normalize_scoring_updates,
    save_scoring_updates,
)
from utils.auth import authenticate_admin, role_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PRESENTATION_TIME_LIMIT_KEY = "presentation_time_limit_seconds"
PRESENTATION_TIMER_STATE_KEY = "presentation_timer_state_v1"
DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS = 300


def _format_duration(total_seconds):
    total_seconds = max(0, int(total_seconds or 0))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _get_system_setting(key):
    return SystemSetting.query.filter_by(key=key).first()


def _set_system_setting(key, value):
    row = _get_system_setting(key)
    if row is None:
        row = SystemSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    return row


def _get_presentation_time_limit_seconds():
    row = _get_system_setting(PRESENTATION_TIME_LIMIT_KEY)
    if not row:
        return DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS

    try:
        parsed_value = int(row.value)
    except (TypeError, ValueError):
        return DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS

    return min(3600, max(60, parsed_value))


def _get_default_timer_state():
    return {
        "running": False,
        "elapsed_seconds": 0,
        "started_at": None,
    }


def _normalize_timer_state(raw_state):
    if not isinstance(raw_state, dict):
        return _get_default_timer_state()

    state = _get_default_timer_state()
    state["running"] = bool(raw_state.get("running", False))

    try:
        state["elapsed_seconds"] = max(0, int(raw_state.get("elapsed_seconds", 0)))
    except (TypeError, ValueError):
        state["elapsed_seconds"] = 0

    started_at = raw_state.get("started_at")
    state["started_at"] = started_at if isinstance(started_at, str) else None
    return state


def _get_timer_state_payload():
    row = _get_system_setting(PRESENTATION_TIMER_STATE_KEY)
    if not row:
        return _get_default_timer_state()

    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _get_default_timer_state()

    return _normalize_timer_state(parsed)


def _compute_timer_elapsed_seconds(timer_state, now_utc=None):
    state = _normalize_timer_state(timer_state)
    elapsed = int(state["elapsed_seconds"])
    if not state["running"]:
        return elapsed

    started_at_iso = state.get("started_at")
    if not started_at_iso:
        return elapsed

    try:
        started_at = datetime.fromisoformat(started_at_iso)
    except ValueError:
        return elapsed

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    now_utc = now_utc or _utcnow()
    delta_seconds = int((now_utc - started_at).total_seconds())
    return max(0, elapsed + max(0, delta_seconds))


def _save_timer_state(timer_state):
    normalized = _normalize_timer_state(timer_state)
    _set_system_setting(PRESENTATION_TIMER_STATE_KEY, json.dumps(normalized))
    return normalized


def _timer_state_snapshot(now_utc=None):
    now_utc = now_utc or _utcnow()
    time_limit_seconds = _get_presentation_time_limit_seconds()
    state = _get_timer_state_payload()
    elapsed_seconds = _compute_timer_elapsed_seconds(state, now_utc=now_utc)
    overtime_seconds = max(0, elapsed_seconds - time_limit_seconds)

    return {
        "running": bool(state.get("running")),
        "elapsed_seconds": elapsed_seconds,
        "elapsed_text": _format_duration(elapsed_seconds),
        "time_limit_seconds": time_limit_seconds,
        "time_limit_text": _format_duration(time_limit_seconds),
        "overtime_seconds": overtime_seconds,
        "overtime_text": _format_duration(overtime_seconds),
    }


@admin_bp.get("/dashboard")
@role_required("admin")
def dashboard():
    stats = {
        "teams": db.session.query(Team).count(),
        "judges": (
            db.session.query(Judge)
            .join(User, User.id == Judge.user_id)
            .filter(User.role == "judge", User.is_active.is_(True), Judge.is_active.is_(True))
            .count()
        ),
        "pending_requests": db.session.query(JudgeLoginRequest).filter_by(status=LOGIN_REQUEST_STATUS_PENDING).count(),
        "total_scores": db.session.query(Score).count()
    }
    return render_template("admin/dashboard.html", stats=stats)


def _seed_defaults_after_kill_switch():
    db.session.add(ProcessOption(name="General"))
    db.session.add(ThemeOption(name="General"))
    db.session.add(
        SystemSetting(
            key=PRESENTATION_TIME_LIMIT_KEY,
            value=str(DEFAULT_PRESENTATION_TIME_LIMIT_SECONDS),
        )
    )
    db.session.add(
        SystemSetting(
            key=PRESENTATION_TIMER_STATE_KEY,
            value=json.dumps(_get_default_timer_state()),
        )
    )

    for category_key, defaults in DEFAULT_SCORING_RULES.items():
        db.session.add(
            ScoringCategorySetting(
                category=category_key,
                weight_percent=defaults["weight_percent"],
                max_score=defaults["max_score"],
            )
        )


@admin_bp.post("/kill-switch/wipe-database")
@role_required("admin")
def kill_switch_wipe_database():
    admin_password = request.form.get("admin_password", "")
    admin_username = getattr(current_user, "username", "")

    if not admin_password:
        flash("Admin password is required.", "warning")
        return redirect(url_for("admin.dashboard"))

    if not authenticate_admin(admin_username, admin_password):
        flash("Invalid admin password. Kill switch cancelled.", "danger")
        return redirect(url_for("admin.dashboard"))

    table_names = [table.name for table in db.metadata.sorted_tables if table.name]
    quoted_table_list = ", ".join(f'"{name}"' for name in table_names)

    try:
        if quoted_table_list:
            db.session.execute(text(f"TRUNCATE TABLE {quoted_table_list} RESTART IDENTITY CASCADE"))

        _seed_defaults_after_kill_switch()
        db.session.commit()
        current_app.logger.warning("Kill switch executed by admin user '%s'.", admin_username)
        flash("Kill switch completed. Database wiped and defaults restored.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Kill switch execution failed: %s", exc)
        flash("Kill switch failed. No changes were saved.", "danger")

    return redirect(url_for("admin.dashboard"))


@admin_bp.get("/options")
@role_required("admin")
def manage_options():
    teams = Team.query.order_by(Team.team_name.asc()).all()
    judges = (
        db.session.query(Judge)
        .join(User, User.id == Judge.user_id)
        .filter(User.role == "judge")
        .order_by(Judge.display_name.asc())
        .all()
    )
    themes = ThemeOption.query.order_by(ThemeOption.name.asc()).all()
    processes = ProcessOption.query.order_by(ProcessOption.name.asc()).all()
    scoring_definitions = get_category_definitions()
    presentation_time_limit_seconds = _get_presentation_time_limit_seconds()

    return render_template(
        "admin/options.html",
        teams=teams,
        judges=judges,
        themes=themes,
        processes=processes,
        scoring_definitions=scoring_definitions,
        presentation_time_limit_seconds=presentation_time_limit_seconds,
    )


@admin_bp.post("/options/themes")
@role_required("admin")
def add_theme_option():
    theme_name = request.form.get("theme_name", "").strip()
    if not theme_name:
        flash("Theme name is required.", "warning")
        return redirect(url_for("admin.manage_options"))

    try:
        existing = ThemeOption.query.filter_by(name=theme_name).first()
        if existing:
            flash("Theme already exists.", "warning")
            return redirect(url_for("admin.manage_options"))

        db.session.add(ThemeOption(name=theme_name))
        db.session.commit()
        flash("Theme added successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Add theme option failed: %s", exc)
        flash("Unable to add theme.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/themes/<int:theme_id>/delete")
@role_required("admin")
def delete_theme_option(theme_id):
    try:
        theme = ThemeOption.query.filter_by(id=theme_id).first()
        if not theme:
            flash("Theme not found.", "warning")
            return redirect(url_for("admin.manage_options"))

        db.session.delete(theme)
        db.session.commit()
        flash("Theme deleted successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Delete theme option failed: %s", exc)
        flash("Unable to delete theme.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/processes")
@role_required("admin")
def add_process_option():
    process_name = request.form.get("process_name", "").strip()
    if not process_name:
        flash("Process name is required.", "warning")
        return redirect(url_for("admin.manage_options"))

    try:
        existing = ProcessOption.query.filter_by(name=process_name).first()
        if existing:
            flash("Process already exists.", "warning")
            return redirect(url_for("admin.manage_options"))

        db.session.add(ProcessOption(name=process_name))
        db.session.commit()
        flash("Process added successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Add process option failed: %s", exc)
        flash("Unable to add process.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/processes/<int:process_id>/delete")
@role_required("admin")
def delete_process_option(process_id):
    try:
        process = ProcessOption.query.filter_by(id=process_id).first()
        if not process:
            flash("Process not found.", "warning")
            return redirect(url_for("admin.manage_options"))

        db.session.delete(process)
        db.session.commit()
        flash("Process deleted successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Delete process option failed: %s", exc)
        flash("Unable to delete process.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/scores/delete")
@role_required("admin")
def delete_scores():
    team_id_raw = request.form.get("team_id", "").strip()
    judge_ids_raw = request.form.getlist("judge_ids")

    judge_ids = []
    for raw in judge_ids_raw:
        try:
            judge_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    try:
        query = Score.query
        filters_applied = []

        if team_id_raw and team_id_raw != "all":
            try:
                team_id = int(team_id_raw)
                query = query.filter(Score.team_id == team_id)
                filters_applied.append("team")
            except ValueError:
                flash("Invalid team selected for score deletion.", "warning")
                return redirect(url_for("admin.manage_options"))

        if judge_ids:
            query = query.filter(Score.judge_id.in_(judge_ids))
            filters_applied.append("judges")

        if not filters_applied:
            flash("Select at least one filter: team or judge.", "warning")
            return redirect(url_for("admin.manage_options"))

        deleted_count = query.delete(synchronize_session=False)

        db.session.add(
            AuditLog(
                actor_user_id=current_user.id,
                action="scores_bulk_deleted",
                entity_type="scores",
                entity_id=None,
                old_data={
                    "team_id": team_id_raw if team_id_raw else None,
                    "judge_ids": judge_ids,
                },
                new_data={"deleted_count": deleted_count},
            )
        )

        db.session.commit()
        flash(f"Deleted {deleted_count} score rows successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Bulk score delete failed: %s", exc)
        flash("Unable to delete scores.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/scoring")
@role_required("admin")
def update_scoring_options():
    try:
        updates = normalize_scoring_updates(request.form)
        save_scoring_updates(updates)
        flash("Scoring limits and percentages updated successfully.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Scoring options update failed: %s", exc)
        flash("Unable to update scoring options.", "danger")

    return redirect(url_for("admin.manage_options"))


@admin_bp.post("/options/presentation-time-limit")
@role_required("admin")
def update_presentation_time_limit_option():
    raw_limit = (request.form.get("presentation_time_limit_seconds") or "").strip()
    try:
        time_limit_seconds = int(raw_limit)
    except (TypeError, ValueError):
        flash("Presentation time limit must be a whole number of seconds.", "warning")
        return redirect(url_for("admin.manage_options"))

    if time_limit_seconds < 60 or time_limit_seconds > 3600:
        flash("Presentation time limit must be between 60 and 3600 seconds.", "warning")
        return redirect(url_for("admin.manage_options"))

    try:
        _set_system_setting(PRESENTATION_TIME_LIMIT_KEY, str(time_limit_seconds))
        db.session.commit()
        flash(f"Presentation time limit updated to {_format_duration(time_limit_seconds)}.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Presentation time limit update failed: %s", exc)
        flash("Unable to update presentation time limit.", "danger")

    return redirect(url_for("admin.manage_options"))


def _validate_optional_url(label, raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be a valid URL starting with http:// or https://")

    return value


def _normalize_name_token(raw_value):
    value = (raw_value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return normalized or "user"


def _generate_internal_email(name_token):
    return f"{name_token}_{uuid.uuid4().hex[:8]}@internal.local"


def _generate_unique_username_from_name(display_name):
    base = _normalize_name_token(display_name)
    candidate = base
    suffix = 1

    while User.query.filter_by(username=candidate).first() is not None:
        suffix += 1
        candidate = f"{base}_{suffix}"

    return candidate


def _utcnow():
    return datetime.now(timezone.utc)


def _admin_actor_name():
    return getattr(current_user, "username", "admin")


def _get_pending_login_requests(limit=25):
    return (
        db.session.query(JudgeLoginRequest, Judge, User)
        .join(Judge, Judge.id == JudgeLoginRequest.judge_id)
        .join(User, User.id == Judge.user_id)
        .filter(
            JudgeLoginRequest.status == LOGIN_REQUEST_STATUS_PENDING,
            Judge.is_active.is_(True),
            User.is_active.is_(True),
            User.role == "judge",
        )
        .order_by(JudgeLoginRequest.created_at.asc())
        .limit(limit)
        .all()
    )


def _active_direct_links_by_judge(now_utc):
    links = (
        JudgeDirectLoginLink.query.filter(
            JudgeDirectLoginLink.revoked_at.is_(None),
            JudgeDirectLoginLink.expires_at > now_utc,
        )
        .order_by(JudgeDirectLoginLink.created_at.desc())
        .all()
    )

    link_map = {}
    for link in links:
        link_map.setdefault(link.judge_id, []).append(link)

    return link_map


def _active_direct_links_by_team(now_utc):
    links = (
        TeamDirectLoginLink.query.filter(
            TeamDirectLoginLink.revoked_at.is_(None),
            TeamDirectLoginLink.expires_at > now_utc,
        )
        .order_by(TeamDirectLoginLink.created_at.desc())
        .all()
    )

    link_map = {}
    for link in links:
        link_map.setdefault(link.team_id, []).append(link)

    return link_map


def _get_theme_and_process_names():
    theme_names = [item.name for item in ThemeOption.query.order_by(ThemeOption.name.asc()).all()]
    process_names = [item.name for item in ProcessOption.query.order_by(ProcessOption.name.asc()).all()]
    return theme_names, process_names


def _get_active_teams_for_presentation():
    return (
        Team.query.options(
            joinedload(Team.project),
            joinedload(Team.members),
        )
        .filter(Team.is_active.is_(True))
        .order_by(Team.sort_order.asc(), Team.id.asc())
        .all()
    )


def _find_default_presentation_team(teams):
    for team in teams:
        if not team.presentation_completed:
            return team

    return teams[0] if teams else None


def _find_adjacent_team_ids(teams, current_team_id):
    if not current_team_id:
        return None, None

    team_ids = [team.id for team in teams]
    if current_team_id not in team_ids:
        return None, None

    current_index = team_ids.index(current_team_id)
    previous_team_id = team_ids[current_index - 1] if current_index > 0 else None
    next_team_id = team_ids[current_index + 1] if current_index + 1 < len(team_ids) else None
    return previous_team_id, next_team_id


def _find_next_pending_team(teams, current_team_id):
    if not teams:
        return None

    team_ids = [team.id for team in teams]
    current_index = team_ids.index(current_team_id) if current_team_id in team_ids else -1

    for team in teams[current_index + 1 :]:
        if not team.presentation_completed:
            return team

    for team in teams:
        if not team.presentation_completed:
            return team

    return None


def _parse_team_form_payload(form_data):
    team_name = form_data.get("team_name", "").strip()
    process = form_data.get("process", "").strip()
    theme = form_data.get("theme", "").strip()
    project_title = form_data.get("project_title", "").strip()
    problem_statement = form_data.get("problem_statement", "").strip()
    project_summary = form_data.get("project_summary", "").strip()

    if not team_name or not process or not theme or not project_title or not problem_statement or not project_summary:
        raise ValueError(
            "Team name, process, theme, project title, problem statement, and project summary are required."
        )

    return {
        "team_name": team_name,
        "process": process,
        "theme": theme,
        "project_title": project_title,
        "problem_statement": problem_statement,
        "project_summary": project_summary,
        "repository_url": _validate_optional_url("Repository URL", form_data.get("repository_url", "")),
        "demo_url": _validate_optional_url("Demo URL", form_data.get("demo_url", "")),
        "notes_url": _validate_optional_url("Notes URL", form_data.get("notes_url", "")),
        "is_active": form_data.get("is_active") == "on",
    }


@admin_bp.get("/teams")
@role_required("admin")
def list_teams():
    now_utc = _utcnow()
    try:
        teams = (
            Team.query.options(
                joinedload(Team.project),
                joinedload(Team.members),
            )
            .order_by(Team.sort_order.asc(), Team.id.asc())
            .all()
        )
        active_team_links_by_team = _active_direct_links_by_team(now_utc)
    except SQLAlchemyError as exc:
        current_app.logger.error("Team list query failed: %s", exc)
        flash("Unable to load teams. Ensure database schema is applied.", "warning")
        teams = []
        active_team_links_by_team = {}

    return render_template(
        "admin/teams.html",
        teams=teams,
        active_team_links_by_team=active_team_links_by_team,
        now_utc=now_utc,
    )


@admin_bp.get("/presentation")
@role_required("admin")
def presentation_control():
    team_id_raw = (request.args.get("team_id") or "").strip()
    selected_team_id = None
    if team_id_raw:
        try:
            selected_team_id = int(team_id_raw)
        except ValueError:
            flash("Invalid team selected for presentation control.", "warning")

    try:
        teams = _get_active_teams_for_presentation()
    except SQLAlchemyError as exc:
        current_app.logger.error("Presentation team list query failed: %s", exc)
        flash("Unable to load presentation queue.", "danger")
        teams = []

    current_team = None
    if selected_team_id:
        current_team = next((team for team in teams if team.id == selected_team_id), None)
        if current_team is None:
            flash("Selected team is not available in the active presentation queue.", "warning")

    if current_team is None:
        current_team = _find_default_presentation_team(teams)

    previous_team_id, next_team_id = _find_adjacent_team_ids(teams, current_team.id if current_team else None)
    next_pending_team = _find_next_pending_team(teams, current_team.id if current_team else None)
    if current_team and next_pending_team and next_pending_team.id == current_team.id:
        next_pending_team = None

    pending_count = sum(1 for team in teams if not team.presentation_completed)
    completed_count = len(teams) - pending_count
    timer_snapshot = _timer_state_snapshot()

    return render_template(
        "admin/presentation.html",
        teams=teams,
        current_team=current_team,
        next_pending_team=next_pending_team,
        previous_team_id=previous_team_id,
        next_team_id=next_team_id,
        pending_count=pending_count,
        completed_count=completed_count,
        timer_snapshot=timer_snapshot,
    )


@admin_bp.get("/presentation/timer/state")
@role_required("admin", "judge")
def presentation_timer_state():
    try:
        teams = _get_active_teams_for_presentation()
        current_team = _find_default_presentation_team(teams)
        next_pending_team = _find_next_pending_team(teams, current_team.id if current_team else None)
        if current_team and next_pending_team and current_team.id == next_pending_team.id:
            next_pending_team = None

        snapshot = _timer_state_snapshot()
        return jsonify(
            {
                "ok": True,
                **snapshot,
                "current_team_name": current_team.team_name if current_team else None,
                "next_team_name": next_pending_team.team_name if next_pending_team else None,
            }
        )
    except SQLAlchemyError as exc:
        current_app.logger.error("Presentation timer state load failed: %s", exc)
        return jsonify({"ok": False, "error": "Unable to load presentation timer state."}), 500


@admin_bp.post("/presentation/timer/control")
@role_required("admin")
def control_presentation_timer():
    payload = request.get_json(silent=True) or request.form
    action = (payload.get("action") or "").strip().lower()
    if action not in {"start", "pause", "reset"}:
        return jsonify({"ok": False, "error": "Invalid timer action."}), 400

    now_utc = _utcnow()
    state = _get_timer_state_payload()
    elapsed_seconds = _compute_timer_elapsed_seconds(state, now_utc=now_utc)

    if action == "start":
        if not bool(state.get("running")):
            state["running"] = True
            state["elapsed_seconds"] = elapsed_seconds
            state["started_at"] = now_utc.isoformat()
    elif action == "pause":
        state["running"] = False
        state["elapsed_seconds"] = elapsed_seconds
        state["started_at"] = None
    elif action == "reset":
        state = _get_default_timer_state()

    try:
        _save_timer_state(state)
        db.session.commit()
        return jsonify({"ok": True, **_timer_state_snapshot(now_utc=now_utc)})
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Presentation timer control failed: %s", exc)
        return jsonify({"ok": False, "error": "Unable to update timer state."}), 500


@admin_bp.post("/presentation/<int:team_id>/complete")
@role_required("admin")
def mark_team_presentation_complete(team_id):
    now_utc = _utcnow()
    try:
        team = Team.query.filter_by(id=team_id, is_active=True).first()
        if not team:
            flash("Team not found or inactive.", "warning")
            return redirect(url_for("admin.presentation_control"))

        timer_elapsed_seconds = _compute_timer_elapsed_seconds(_get_timer_state_payload(), now_utc=now_utc)
        captured_duration_text = _format_duration(timer_elapsed_seconds)

        if not team.presentation_completed:
            team.presentation_completed = True
            team.presentation_completed_at = now_utc
            team.presentation_elapsed_seconds = timer_elapsed_seconds

            # Prepare timer for the next team; admin manually starts when ready.
            _save_timer_state(_get_default_timer_state())
            db.session.commit()
        else:
            flash(f"{team.team_name} is already marked completed.", "info")
            return redirect(url_for("admin.presentation_control", team_id=team.id))

        teams = _get_active_teams_for_presentation()
        next_pending_team = _find_next_pending_team(teams, team.id)
        if next_pending_team is not None:
            flash(
                f"{team.team_name} marked done in {captured_duration_text}. Move to next team: {next_pending_team.team_name}. Timer reset; start when ready.",
                "success",
            )
            return redirect(url_for("admin.presentation_control", team_id=next_pending_team.id))

        flash(
            f"{team.team_name} marked done in {captured_duration_text}. All pending teams are completed.",
            "success",
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Mark team presentation complete failed: %s", exc)
        flash("Unable to mark team as completed.", "danger")

    return redirect(url_for("admin.presentation_control"))


@admin_bp.post("/presentation/<int:team_id>/reopen")
@role_required("admin")
def reopen_team_presentation(team_id):
    try:
        team = Team.query.filter_by(id=team_id, is_active=True).first()
        if not team:
            flash("Team not found or inactive.", "warning")
            return redirect(url_for("admin.presentation_control"))

        team.presentation_completed = False
        team.presentation_completed_at = None
        team.presentation_elapsed_seconds = None
        db.session.commit()
        flash(f"{team.team_name} moved back to pending queue.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Reopen team presentation failed: %s", exc)
        flash("Unable to reopen team in presentation queue.", "danger")

    return redirect(url_for("admin.presentation_control", team_id=team_id))


@admin_bp.post("/presentation/reset")
@role_required("admin")
def reset_presentation_queue():
    try:
        updated_count = (
            Team.query.filter(Team.presentation_completed.is_(True)).update(
                {
                    Team.presentation_completed: False,
                    Team.presentation_completed_at: None,
                    Team.presentation_elapsed_seconds: None,
                },
                synchronize_session=False,
            )
        )

        _save_timer_state(_get_default_timer_state())
        db.session.commit()
        flash(f"Presentation queue reset for {updated_count} team(s). Timer reset to 00:00.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Reset presentation queue failed: %s", exc)
        flash("Unable to reset presentation queue.", "danger")

    return redirect(url_for("admin.presentation_control"))


@admin_bp.route("/teams/new", methods=["GET", "POST"])
@role_required("admin")
def create_team():
    theme_options, process_options = _get_theme_and_process_names()

    if request.method == "POST":
        try:
            payload = _parse_team_form_payload(request.form)

            existing_team = Team.query.filter_by(team_name=payload["team_name"]).first()
            if existing_team:
                flash("A team with this name already exists.", "warning")
                return render_template(
                    "admin/team_form.html",
                    mode="create",
                    form_data=request.form,
                    theme_options=theme_options,
                    process_options=process_options,
                )

            max_sort_order = db.session.query(db.func.coalesce(db.func.max(Team.sort_order), 0)).scalar() or 0

            team = Team(
                team_name=payload["team_name"],
                sort_order=int(max_sort_order) + 1,
                process=payload["process"],
                theme=payload["theme"],
                is_active=payload["is_active"],
            )
            team.project = Project(
                project_title=payload["project_title"],
                problem_statement=payload["problem_statement"],
                project_summary=payload["project_summary"],
                repository_url=payload["repository_url"],
                demo_url=payload["demo_url"],
                notes_url=payload["notes_url"],
            )

            db.session.add(team)
            db.session.commit()
            flash("Team created successfully.", "success")
            return redirect(url_for("admin.list_teams"))
        except ValueError as exc:
            flash(str(exc), "warning")
        except IntegrityError:
            db.session.rollback()
            flash("Unable to create team due to duplicate values.", "danger")
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Team creation failed: %s", exc)
            flash("Unable to create team.", "danger")

    return render_template(
        "admin/team_form.html",
        mode="create",
        form_data=request.form,
        theme_options=theme_options,
        process_options=process_options,
    )


@admin_bp.post("/teams/reorder")
@role_required("admin")
def reorder_teams():
    payload = request.get_json(silent=True) or {}
    team_ids_raw = payload.get("team_ids")

    if not isinstance(team_ids_raw, list) or not team_ids_raw:
        return jsonify({"error": "team_ids must be a non-empty list."}), 400

    normalized_ids = []
    seen = set()
    for item in team_ids_raw:
        try:
            team_id = int(item)
        except (TypeError, ValueError):
            return jsonify({"error": "team_ids contains non-integer values."}), 400

        if team_id in seen:
            return jsonify({"error": "team_ids contains duplicate values."}), 400

        seen.add(team_id)
        normalized_ids.append(team_id)

    existing_ids = {item[0] for item in db.session.query(Team.id).all()}
    if set(normalized_ids) != existing_ids:
        return jsonify({"error": "team_ids must include all teams exactly once."}), 400

    try:
        team_map = {team.id: team for team in Team.query.filter(Team.id.in_(normalized_ids)).all()}
        for position, team_id in enumerate(normalized_ids, start=1):
            team_map[team_id].sort_order = position

        db.session.commit()
        return jsonify({"ok": True, "updated": len(normalized_ids)})
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Team reorder failed: %s", exc)
        return jsonify({"error": "Unable to save team order."}), 500


@admin_bp.post("/teams/<int:team_id>/access")
@role_required("admin")
def update_team_access(team_id):
    login_id = (request.form.get("portal_login_id") or "").strip()
    password = request.form.get("portal_password", "")

    if not login_id or len(login_id) < 3:
        flash("Team login ID must be at least 3 characters.", "warning")
        return redirect(url_for("admin.list_teams"))

    if len(password) < 8:
        flash("Team portal password must be at least 8 characters.", "warning")
        return redirect(url_for("admin.list_teams"))

    try:
        team = Team.query.filter_by(id=team_id).first()
        if not team:
            flash("Team not found.", "warning")
            return redirect(url_for("admin.list_teams"))

        duplicate_team = Team.query.filter(Team.portal_login_id == login_id, Team.id != team_id).first()
        if duplicate_team:
            flash("This Team Login ID is already in use.", "warning")
            return redirect(url_for("admin.list_teams"))

        team.portal_login_id = login_id
        team.portal_password_hash = generate_password_hash(password)
        db.session.commit()
        flash(f"Team portal credentials updated for {team.team_name}.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Update team access failed: %s", exc)
        flash("Unable to update team access credentials.", "danger")

    return redirect(url_for("admin.list_teams"))


@admin_bp.post("/teams/<int:team_id>/access-link")
@role_required("admin")
def create_team_access_link(team_id):
    lifespan_text = (request.form.get("lifespan_minutes") or "30").strip()

    try:
        lifespan_minutes = int(lifespan_text)
    except ValueError:
        flash("Team link lifespan must be a number of minutes.", "warning")
        return redirect(url_for("admin.list_teams"))

    if lifespan_minutes < 1 or lifespan_minutes > 1440:
        flash("Team link lifespan must be between 1 and 1440 minutes.", "warning")
        return redirect(url_for("admin.list_teams"))

    try:
        team = Team.query.filter_by(id=team_id).first()
        if not team or not team.is_active:
            flash("Team not found or inactive.", "warning")
            return redirect(url_for("admin.list_teams"))

        if not team.portal_login_id or not team.portal_password_hash:
            flash("Set Team Login ID and Password before generating a link.", "warning")
            return redirect(url_for("admin.list_teams"))

        token = secrets.token_urlsafe(32)
        expires_at = _utcnow() + timedelta(minutes=lifespan_minutes)
        link = TeamDirectLoginLink(
            team_id=team.id,
            token=token,
            expires_at=expires_at,
            created_by_admin=_admin_actor_name(),
        )
        db.session.add(link)
        db.session.commit()

        link_url = url_for("public.team_direct_login", token=token, _external=True)
        flash(f"Team access link created for {team.team_name}: {link_url}", "info")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Create team access link failed: %s", exc)
        flash("Unable to create team access link.", "danger")

    return redirect(url_for("admin.list_teams"))


@admin_bp.post("/teams/access-link/<int:link_id>/revoke")
@role_required("admin")
def revoke_team_access_link(link_id):
    try:
        link = TeamDirectLoginLink.query.filter_by(id=link_id).first()
        if not link:
            flash("Team access link not found.", "warning")
            return redirect(url_for("admin.list_teams"))

        if link.revoked_at is not None:
            flash("Team access link is already revoked.", "info")
            return redirect(url_for("admin.list_teams"))

        link.revoked_at = _utcnow()
        link.revoke_reason = "admin_revoked"
        db.session.commit()
        flash("Team access link revoked.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Revoke team access link failed: %s", exc)
        flash("Unable to revoke team access link.", "danger")

    return redirect(url_for("admin.list_teams"))


@admin_bp.route("/teams/<int:team_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_team(team_id):
    team = Team.query.options(joinedload(Team.project)).filter_by(id=team_id).first()
    if not team:
        flash("Team not found.", "warning")
        return redirect(url_for("admin.list_teams"))

    theme_options, process_options = _get_theme_and_process_names()

    if request.method == "POST":
        try:
            payload = _parse_team_form_payload(request.form)

            duplicate_team = Team.query.filter(
                Team.team_name == payload["team_name"],
                Team.id != team.id,
            ).first()
            if duplicate_team:
                flash("Another team already uses that team name.", "warning")
                return render_template(
                    "admin/team_form.html",
                    mode="edit",
                    team=team,
                    form_data=request.form,
                    theme_options=theme_options,
                    process_options=process_options,
                )

            team.team_name = payload["team_name"]
            team.process = payload["process"]
            team.theme = payload["theme"]
            team.is_active = payload["is_active"]

            project = team.project
            if not project:
                project = Project(team=team)
                db.session.add(project)

            project.project_title = payload["project_title"]
            project.problem_statement = payload["problem_statement"]
            project.project_summary = payload["project_summary"]
            project.repository_url = payload["repository_url"]
            project.demo_url = payload["demo_url"]
            project.notes_url = payload["notes_url"]

            db.session.commit()
            flash("Team details updated successfully.", "success")
            return redirect(url_for("admin.list_teams"))
        except ValueError as exc:
            flash(str(exc), "warning")
        except IntegrityError:
            db.session.rollback()
            flash("Unable to update team due to duplicate values.", "danger")
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Team update failed: %s", exc)
            flash("Unable to update team.", "danger")

    return render_template(
        "admin/team_form.html",
        mode="edit",
        team=team,
        form_data=request.form,
        theme_options=theme_options,
        process_options=process_options,
    )


@admin_bp.post("/teams/<int:team_id>/delete")
@role_required("admin")
def delete_team(team_id):
    try:
        team = Team.query.filter_by(id=team_id).first()
        if not team:
            flash("Team not found.", "warning")
            return redirect(url_for("admin.list_teams"))

        db.session.delete(team)
        db.session.commit()
        flash("Team deleted successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Team delete failed: %s", exc)
        flash("Unable to delete team.", "danger")

    return redirect(url_for("admin.list_teams"))


@admin_bp.post("/teams/<int:team_id>/toggle-active")
@role_required("admin")
def toggle_team_active(team_id):
    try:
        team = Team.query.filter_by(id=team_id).first()
        if not team:
            flash("Team not found.", "warning")
            return redirect(url_for("admin.list_teams"))

        team.is_active = not team.is_active
        db.session.commit()
        state = "activated" if team.is_active else "deactivated"
        flash(f"Team {state} successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Team activation toggle failed: %s", exc)
        flash("Unable to update team status.", "danger")

    return redirect(url_for("admin.list_teams"))


@admin_bp.route("/teams/<int:team_id>/members", methods=["GET", "POST"])
@role_required("admin")
def manage_team_members(team_id):
    team = Team.query.options(joinedload(Team.members)).filter_by(id=team_id).first()
    if not team:
        flash("Team not found.", "warning")
        return redirect(url_for("admin.list_teams"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()

        if not full_name:
            flash("Member name is required.", "warning")
            return redirect(url_for("admin.manage_team_members", team_id=team_id))

        try:
            normalized_name = _normalize_name_token(full_name)
            internal_email = _generate_internal_email(normalized_name)

            member = TeamMember(
                team_id=team_id,
                full_name=full_name,
                email=internal_email,
                phone=None,
                department_or_class=None,
            )
            db.session.add(member)
            db.session.commit()
            flash("Team member added successfully.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Unable to add member due to duplicate values.", "danger")
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Add team member failed: %s", exc)
            flash("Unable to add team member.", "danger")

        return redirect(url_for("admin.manage_team_members", team_id=team_id))

    members = sorted(team.members, key=lambda m: m.id)
    return render_template("admin/team_members.html", team=team, members=members)


@admin_bp.route("/teams/<int:team_id>/members/<int:member_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_team_member(team_id, member_id):
    team = Team.query.filter_by(id=team_id).first()
    if not team:
        flash("Team not found.", "warning")
        return redirect(url_for("admin.list_teams"))

    member = TeamMember.query.filter_by(id=member_id, team_id=team_id).first()
    if not member:
        flash("Team member not found.", "warning")
        return redirect(url_for("admin.manage_team_members", team_id=team_id))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()

        if not full_name:
            flash("Member name is required.", "warning")
            return render_template("admin/member_form.html", team=team, member=member)

        try:
            member.full_name = full_name

            db.session.commit()
            flash("Team member updated successfully.", "success")
            return redirect(url_for("admin.manage_team_members", team_id=team_id))
        except IntegrityError:
            db.session.rollback()
            flash("Unable to update member due to duplicate values.", "danger")
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Edit team member failed: %s", exc)
            flash("Unable to update team member.", "danger")

    return render_template("admin/member_form.html", team=team, member=member)


@admin_bp.post("/teams/<int:team_id>/members/<int:member_id>/delete")
@role_required("admin")
def delete_team_member(team_id, member_id):
    try:
        member = TeamMember.query.filter_by(id=member_id, team_id=team_id).first()
        if not member:
            flash("Team member not found.", "warning")
            return redirect(url_for("admin.manage_team_members", team_id=team_id))

        db.session.delete(member)
        db.session.commit()
        flash("Team member deleted successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Delete team member failed: %s", exc)
        flash("Unable to delete team member.", "danger")

    return redirect(url_for("admin.manage_team_members", team_id=team_id))


@admin_bp.route("/judges", methods=["GET", "POST"])
@role_required("admin")
def manage_judges():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")

        if not username or not display_name or not password:
            flash("Username, name and password are required.", "warning")
            return redirect(url_for("admin.manage_judges"))

        if not re.fullmatch(r"[a-z0-9_.-]{3,80}", username):
            flash("Username must be 3-80 characters using lowercase letters, numbers, dot, underscore or hyphen.", "warning")
            return redirect(url_for("admin.manage_judges"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "warning")
            return redirect(url_for("admin.manage_judges"))

        try:
            if User.query.filter_by(username=username).first() is not None:
                flash("Username already exists.", "warning")
                return redirect(url_for("admin.manage_judges"))

            internal_email = _generate_internal_email(_normalize_name_token(username))

            user = User(
                username=username,
                email=internal_email,
                password_hash=generate_password_hash(password),
                role="judge",
                is_active=True,
            )
            Judge(
                user=user,
                display_name=display_name,
                phone=None,
                organization=None,
                is_active=True,
            )

            db.session.add(user)
            db.session.commit()
            flash("Judge created successfully.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Unable to create judge due to duplicate data.", "danger")
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.error("Judge creation failed: %s", exc)
            flash(
                "Judge creation failed. Ensure database schema is applied correctly.",
                "danger",
            )

        return redirect(url_for("admin.manage_judges"))

    now_utc = _utcnow()

    try:
        judges = (
            db.session.query(User, Judge)
            .join(Judge, Judge.user_id == User.id)
            .filter(User.role == "judge")
            .order_by(User.id.asc())
            .all()
        )
        judge_ids = [judge.id for _, judge in judges]
        judge_online_map = get_judge_online_map(judge_ids)
        active_links_by_judge = _active_direct_links_by_judge(now_utc)
        pending_login_requests = _get_pending_login_requests(limit=50)
    except SQLAlchemyError as exc:
        current_app.logger.error("Judge list query failed: %s", exc)
        flash("Unable to load judges. Ensure database schema is applied.", "warning")
        judges = []
        judge_online_map = {}
        active_links_by_judge = {}
        pending_login_requests = []

    return render_template(
        "admin/judges.html",
        judges=judges,
        judge_online_map=judge_online_map,
        active_links_by_judge=active_links_by_judge,
        pending_login_requests=pending_login_requests,
        now_utc=now_utc,
    )


@admin_bp.post("/judges/<int:user_id>/password")
@role_required("admin")
def update_judge_password(user_id):
    new_password = request.form.get("new_password", "")

    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "warning")
        return redirect(url_for("admin.manage_judges"))

    try:
        user = User.query.filter_by(id=user_id, role="judge").first()
        if not user or not user.judge_profile:
            flash("Judge not found.", "warning")
            return redirect(url_for("admin.manage_judges"))

        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash(f"Password updated for {user.judge_profile.display_name}.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Judge password update failed: %s", exc)
        flash("Unable to update judge password.", "danger")

    return redirect(url_for("admin.manage_judges"))


@admin_bp.post("/judges/<int:user_id>/direct-link")
@role_required("admin")
def create_judge_direct_link(user_id):
    lifespan_text = (request.form.get("lifespan_minutes") or "15").strip()

    try:
        lifespan_minutes = int(lifespan_text)
    except ValueError:
        flash("Link lifespan must be a number of minutes.", "warning")
        return redirect(url_for("admin.manage_judges"))

    if lifespan_minutes < 1 or lifespan_minutes > 1440:
        flash("Link lifespan must be between 1 and 1440 minutes.", "warning")
        return redirect(url_for("admin.manage_judges"))

    try:
        user = User.query.filter_by(id=user_id, role="judge").first()
        judge = user.judge_profile if user else None
        if not user or not judge or not user.is_active or not judge.is_active:
            flash("Judge not found or inactive.", "warning")
            return redirect(url_for("admin.manage_judges"))

        token = secrets.token_urlsafe(32)
        expires_at = _utcnow() + timedelta(minutes=lifespan_minutes)

        link = JudgeDirectLoginLink(
            judge_id=judge.id,
            token=token,
            expires_at=expires_at,
            created_by_admin=_admin_actor_name(),
        )
        db.session.add(link)
        db.session.commit()

        link_url = url_for("public.judge_direct_login", token=token, _external=True)
        flash(
            f"Direct login link created for {judge.display_name}: {link_url} (valid {lifespan_minutes} minutes)",
            "info",
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Direct link generation failed: %s", exc)
        flash("Unable to create direct login link.", "danger")

    return redirect(url_for("admin.manage_judges"))


@admin_bp.post("/judges/direct-link/<int:link_id>/revoke")
@role_required("admin")
def revoke_judge_direct_link(link_id):
    try:
        link = JudgeDirectLoginLink.query.filter_by(id=link_id).first()
        if not link:
            flash("Direct login link not found.", "warning")
            return redirect(url_for("admin.manage_judges"))

        if link.revoked_at is not None:
            flash("Direct login link is already revoked.", "info")
            return redirect(url_for("admin.manage_judges"))

        link.revoked_at = _utcnow()
        link.revoke_reason = "admin_revoked"
        db.session.commit()
        flash("Direct login link revoked.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Direct link revoke failed: %s", exc)
        flash("Unable to revoke direct login link.", "danger")

    return redirect(url_for("admin.manage_judges"))


@admin_bp.post("/login-requests/<int:request_id>/approve")
@role_required("admin")
def approve_login_request(request_id):
    try:
        login_request = JudgeLoginRequest.query.filter_by(id=request_id).first()
        if not login_request:
            flash("Login request not found.", "warning")
            return redirect(url_for("admin.manage_judges") + "#pending-login-requests")

        if login_request.status != LOGIN_REQUEST_STATUS_PENDING:
            flash("Only pending requests can be approved.", "warning")
            return redirect(url_for("admin.manage_judges") + "#pending-login-requests")

        now_utc = _utcnow()
        login_request.status = LOGIN_REQUEST_STATUS_APPROVED
        login_request.decided_at = now_utc
        login_request.decided_by_admin = _admin_actor_name()
        login_request.approval_expires_at = now_utc + timedelta(minutes=10)
        db.session.commit()
        flash("Login request approved. Judge can now login directly.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Approve login request failed: %s", exc)
        flash("Unable to approve login request.", "danger")

    return redirect(url_for("admin.manage_judges") + "#pending-login-requests")


@admin_bp.post("/login-requests/<int:request_id>/reject")
@role_required("admin")
def reject_login_request(request_id):
    try:
        login_request = JudgeLoginRequest.query.filter_by(id=request_id).first()
        if not login_request:
            flash("Login request not found.", "warning")
            return redirect(url_for("admin.manage_judges") + "#pending-login-requests")

        if login_request.status != LOGIN_REQUEST_STATUS_PENDING:
            flash("Only pending requests can be rejected.", "warning")
            return redirect(url_for("admin.manage_judges") + "#pending-login-requests")

        login_request.status = LOGIN_REQUEST_STATUS_REJECTED
        login_request.decided_at = _utcnow()
        login_request.decided_by_admin = _admin_actor_name()
        login_request.approval_expires_at = None
        db.session.commit()
        flash("Login request rejected.", "info")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Reject login request failed: %s", exc)
        flash("Unable to reject login request.", "danger")

    return redirect(url_for("admin.manage_judges") + "#pending-login-requests")


@admin_bp.get("/notifications/login-requests")
@role_required("admin")
def login_request_notifications():
    try:
        now_utc = _utcnow()

        expired_rows = JudgeLoginRequest.query.filter(
            JudgeLoginRequest.status == LOGIN_REQUEST_STATUS_APPROVED,
            JudgeLoginRequest.approval_expires_at.isnot(None),
            JudgeLoginRequest.approval_expires_at <= now_utc,
            JudgeLoginRequest.consumed_at.is_(None),
        ).all()
        for item in expired_rows:
            item.status = LOGIN_REQUEST_STATUS_EXPIRED
            item.decided_at = now_utc

        if expired_rows:
            db.session.commit()

        rows = _get_pending_login_requests(limit=10)
        payload_items = [
            {
                "request_id": login_request.id,
                "judge_name": judge.display_name,
                "login_key": user.username,
                "requested_login": login_request.requested_login,
                "created_at": login_request.created_at.isoformat() if login_request.created_at else None,
            }
            for login_request, judge, user in rows
        ]

        return jsonify({"count": len(payload_items), "items": payload_items})
    except SQLAlchemyError as exc:
        current_app.logger.error("Login request notifications failed: %s", exc)
        return jsonify({"count": 0, "items": []}), 500


@admin_bp.get("/notifications/judge-presence")
@role_required("admin")
def judge_presence_notifications():
    try:
        judge_ids = [item[0] for item in db.session.query(Judge.id).all()]
        online_map = get_judge_online_map(judge_ids)
        payload = {str(judge_id): bool(status) for judge_id, status in online_map.items()}
        return jsonify({"online": payload})
    except SQLAlchemyError as exc:
        current_app.logger.error("Judge presence notifications failed: %s", exc)
        return jsonify({"online": {}}), 500


@admin_bp.post("/judges/<int:user_id>/delete")
@role_required("admin")
def delete_judge(user_id):
    try:
        user = User.query.filter_by(id=user_id, role="judge").first()
        if not user:
            flash("Judge not found.", "warning")
            return redirect(url_for("admin.manage_judges"))

        db.session.delete(user)
        db.session.commit()
        flash("Judge deleted successfully.", "success")
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Judge delete failed: %s", exc)
        flash("Unable to delete judge.", "danger")

    return redirect(url_for("admin.manage_judges"))
