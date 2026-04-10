from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from models import db
from models.audit import AuditLog
from models.score import Score
from models.team import Team
from models.user import Judge
from services.judge_scoring_service import (
    CATEGORY_COUNT,
    calculate_total_from_raw_scores,
    get_category_definitions,
    get_adjacent_active_team_ids,
    get_judge_dashboard_rows,
    get_judge_team_score_snapshot,
    get_next_active_team_id,
    is_judge_team_locked,
    save_or_update_judge_scores,
)
from services.presence_service import mark_judge_online
from utils.auth import role_required

judge_bp = Blueprint("judge", __name__, url_prefix="/judge")


def _get_current_judge_profile():
    judge_profile = getattr(current_user, "judge_profile", None)
    if judge_profile and judge_profile.is_active:
        return judge_profile

    return Judge.query.filter_by(user_id=current_user.id, is_active=True).first()


@judge_bp.get("/dashboard")
@role_required("judge")
def dashboard():
    judge_profile = _get_current_judge_profile()
    if not judge_profile:
        flash("Your judge profile is unavailable. Contact admin.", "danger")
        return redirect(url_for("public.logout"))

    try:
        mark_judge_online(judge_profile.id)
        teams = get_judge_dashboard_rows(judge_profile.id)
    except SQLAlchemyError as exc:
        current_app.logger.error("Judge dashboard load failed: %s", exc)
        flash("Unable to load teams for scoring.", "danger")
        teams = []

    return render_template(
        "judge/dashboard.html",
        judge=current_user,
        judge_display_name=judge_profile.display_name,
        teams=teams,
        category_count=CATEGORY_COUNT,
    )


@judge_bp.route("/teams/<int:team_id>/score", methods=["GET", "POST"])
@role_required("judge")
def score_team(team_id):
    judge_profile = _get_current_judge_profile()
    if not judge_profile:
        flash("Your judge profile is unavailable. Contact admin.", "danger")
        return redirect(url_for("public.logout"))

    team = Team.query.options(joinedload(Team.project)).filter_by(id=team_id, is_active=True).first()
    if not team:
        flash("Team not found or inactive.", "warning")
        return redirect(url_for("judge.dashboard"))

    mark_judge_online(judge_profile.id)

    category_definitions = get_category_definitions()
    snapshot = get_judge_team_score_snapshot(judge_profile.id, team.id)
    previous_team_id, next_team_id = get_adjacent_active_team_ids(team.id)
    score_values = snapshot["score_values"].copy()
    remarks = snapshot["remarks"]

    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "clear":
            if is_judge_team_locked(judge_profile.id, team.id):
                flash("Scores are locked for this team and cannot be cleared.", "warning")
                return redirect(url_for("judge.score_team", team_id=team.id))

            try:
                deleted_rows = (
                    Score.query.filter_by(judge_id=judge_profile.id, team_id=team.id)
                    .delete(synchronize_session=False)
                )

                if deleted_rows > 0:
                    db.session.add(
                        AuditLog(
                            actor_user_id=current_user.id,
                            action="score_cleared",
                            entity_type="judge_team_scores",
                            entity_id=team.id,
                            old_data={
                                "judge_id": judge_profile.id,
                                "team_id": team.id,
                                "cleared_rows": deleted_rows,
                            },
                            new_data={
                                "judge_id": judge_profile.id,
                                "team_id": team.id,
                                "cleared_rows": 0,
                            },
                        )
                    )
                    flash("All saved scores were cleared for this team.", "success")
                else:
                    flash("No saved scores found for this team.", "info")

                db.session.commit()
            except SQLAlchemyError as exc:
                db.session.rollback()
                current_app.logger.error("Clear judge scores failed: %s", exc)
                flash("Unable to clear scores right now. Try again.", "danger")

            return redirect(url_for("judge.score_team", team_id=team.id))

        if is_judge_team_locked(judge_profile.id, team.id):
            flash("Scores are locked for this team and cannot be edited.", "warning")
            return redirect(url_for("judge.score_team", team_id=team.id))

        raw_scores = {item["key"]: request.form.get(item["key"], "").strip() for item in category_definitions}
        remarks = request.form.get("remarks", "").strip()
        lock_after_save = action == "save_lock"

        try:
            for item in category_definitions:
                score_text = raw_scores[item["key"]]
                if not score_text:
                    raise ValueError(f"{item['label']} is required.")

            save_or_update_judge_scores(
                judge_id=judge_profile.id,
                team_id=team.id,
                raw_scores=raw_scores,
                remarks=remarks,
                actor_user_id=current_user.id,
                lock_after_save=lock_after_save,
            )

            if lock_after_save:
                flash("Scores saved and locked successfully.", "success")
            else:
                flash("Scores saved successfully.", "success")

            if action == "save_next":
                next_team_id = get_next_active_team_id(team.id)
                if next_team_id:
                    return redirect(url_for("judge.score_team", team_id=next_team_id))
                flash("No next active team found. Continue from the dashboard.", "info")

            return redirect(url_for("judge.score_team", team_id=team.id))
        except ValueError as exc:
            flash(str(exc), "warning")
            score_values = {}
            for key, value in raw_scores.items():
                if value in {None, ""}:
                    score_values[key] = None
                    continue
                try:
                    score_values[key] = float(value)
                except (TypeError, ValueError):
                    score_values[key] = None
        except SQLAlchemyError as exc:
            current_app.logger.error("Save judge scores failed: %s", exc)
            flash("Unable to save scores right now. Try again.", "danger")

    preview_raw_scores = {
        key: (value if value is not None else 0) for key, value in score_values.items()
    }

    return render_template(
        "judge/score_team.html",
        judge=current_user,
        team=team,
        category_definitions=category_definitions,
        score_values=score_values,
        remarks=remarks,
        existing_snapshot=snapshot,
        preview_total=calculate_total_from_raw_scores(preview_raw_scores),
        previous_team_id=previous_team_id,
        next_team_id=next_team_id,
    )


@judge_bp.post("/presence/heartbeat")
@role_required("judge")
def heartbeat():
    judge_profile = _get_current_judge_profile()
    if not judge_profile:
        return {"ok": False}, 404

    mark_judge_online(judge_profile.id)
    return {"ok": True}, 200
