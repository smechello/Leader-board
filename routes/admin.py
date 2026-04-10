from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
import re
import uuid
from werkzeug.security import generate_password_hash
from urllib.parse import urlparse

from models import db
from models.audit import AuditLog
from models.options import ProcessOption, ThemeOption
from models.score import Score
from models.team import Project, Team, TeamMember
from models.user import Judge, User
from utils.auth import role_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.get("/dashboard")
@role_required("admin")
def dashboard():
    return render_template("admin/dashboard.html")


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

    return render_template(
        "admin/options.html",
        teams=teams,
        judges=judges,
        themes=themes,
        processes=processes,
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
    delete_all = request.form.get("delete_all") == "on"
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

        if not delete_all:
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
                flash("Select delete all, a team, or one or more judges.", "warning")
                return redirect(url_for("admin.manage_options"))

        deleted_count = query.delete(synchronize_session=False)

        db.session.add(
            AuditLog(
                actor_user_id=current_user.id,
                action="scores_bulk_deleted",
                entity_type="scores",
                entity_id=None,
                old_data={
                    "delete_all": delete_all,
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


def _get_theme_and_process_names():
    theme_names = [item.name for item in ThemeOption.query.order_by(ThemeOption.name.asc()).all()]
    process_names = [item.name for item in ProcessOption.query.order_by(ProcessOption.name.asc()).all()]
    return theme_names, process_names


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
    try:
        teams = (
            Team.query.options(
                joinedload(Team.project),
                joinedload(Team.members),
            )
            .order_by(Team.id.asc())
            .all()
        )
    except SQLAlchemyError as exc:
        current_app.logger.error("Team list query failed: %s", exc)
        flash("Unable to load teams. Ensure database schema is applied.", "warning")
        teams = []

    return render_template("admin/teams.html", teams=teams)


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

            team = Team(
                team_name=payload["team_name"],
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
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")

        if not display_name or not password:
            flash("Judge name and password are required.", "warning")
            return redirect(url_for("admin.manage_judges"))

        try:
            username = _generate_unique_username_from_name(display_name)
            internal_email = _generate_internal_email(_normalize_name_token(display_name))

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

    try:
        judges = (
            db.session.query(User, Judge)
            .join(Judge, Judge.user_id == User.id)
            .filter(User.role == "judge")
            .order_by(User.id.asc())
            .all()
        )
    except SQLAlchemyError as exc:
        current_app.logger.error("Judge list query failed: %s", exc)
        flash("Unable to load judges. Ensure database schema is applied.", "warning")
        judges = []

    return render_template("admin/judges.html", judges=judges)


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
