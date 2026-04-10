from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash
from urllib.parse import urlparse

from models import db
from models.team import Project, Team, TeamMember
from models.user import Judge, User
from utils.auth import role_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.get("/dashboard")
@role_required("admin")
def dashboard():
    return render_template("admin/dashboard.html")


def _validate_optional_url(label, raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be a valid URL starting with http:// or https://")

    return value


def _parse_team_form_payload(form_data):
    team_name = form_data.get("team_name", "").strip()
    theme = form_data.get("theme", "").strip()
    project_title = form_data.get("project_title", "").strip()
    problem_statement = form_data.get("problem_statement", "").strip()
    project_summary = form_data.get("project_summary", "").strip()

    if not team_name or not theme or not project_title or not problem_statement or not project_summary:
        raise ValueError(
            "Team name, theme, project title, problem statement, and project summary are required."
        )

    return {
        "team_name": team_name,
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
    if request.method == "POST":
        try:
            payload = _parse_team_form_payload(request.form)

            existing_team = Team.query.filter_by(team_name=payload["team_name"]).first()
            if existing_team:
                flash("A team with this name already exists.", "warning")
                return render_template("admin/team_form.html", mode="create", form_data=request.form)

            team = Team(
                team_name=payload["team_name"],
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

    return render_template("admin/team_form.html", mode="create", form_data=request.form)


@admin_bp.route("/teams/<int:team_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_team(team_id):
    team = Team.query.options(joinedload(Team.project)).filter_by(id=team_id).first()
    if not team:
        flash("Team not found.", "warning")
        return redirect(url_for("admin.list_teams"))

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
                )

            team.team_name = payload["team_name"]
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

    return render_template("admin/team_form.html", mode="edit", team=team, form_data=request.form)


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
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        department_or_class = request.form.get("department_or_class", "").strip()

        if not full_name or not email:
            flash("Member full name and email are required.", "warning")
            return redirect(url_for("admin.manage_team_members", team_id=team_id))

        try:
            duplicate_member = TeamMember.query.filter_by(team_id=team_id, email=email).first()
            if duplicate_member:
                flash("A member with this email already exists in the team.", "warning")
                return redirect(url_for("admin.manage_team_members", team_id=team_id))

            member = TeamMember(
                team_id=team_id,
                full_name=full_name,
                email=email,
                phone=phone or None,
                department_or_class=department_or_class or None,
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
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        department_or_class = request.form.get("department_or_class", "").strip()

        if not full_name or not email:
            flash("Member full name and email are required.", "warning")
            return render_template("admin/member_form.html", team=team, member=member)

        try:
            duplicate_member = TeamMember.query.filter(
                TeamMember.team_id == team_id,
                TeamMember.email == email,
                TeamMember.id != member_id,
            ).first()
            if duplicate_member:
                flash("Another member in this team already uses that email.", "warning")
                return render_template("admin/member_form.html", team=team, member=member)

            member.full_name = full_name
            member.email = email
            member.phone = phone or None
            member.department_or_class = department_or_class or None

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
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        organization = request.form.get("organization", "").strip()

        if not username or not email or not display_name or not password:
            flash("Username, email, display name, and password are required.", "warning")
            return redirect(url_for("admin.manage_judges"))

        try:
            existing_user = User.query.filter(
                or_(User.username == username, User.email == email)
            ).first()

            if existing_user:
                flash("A user with this username or email already exists.", "warning")
                return redirect(url_for("admin.manage_judges"))

            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role="judge",
                is_active=True,
            )
            Judge(
                user=user,
                display_name=display_name,
                phone=phone or None,
                organization=organization or None,
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
