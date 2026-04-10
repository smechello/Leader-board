from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from models.team import Team
from models.user import Judge
from services.judge_scoring_service import (
    CATEGORY_COUNT,
    CATEGORY_DEFINITIONS,
    calculate_total_from_raw_scores,
    get_judge_dashboard_rows,
    get_judge_team_score_snapshot,
    get_next_active_team_id,
    save_or_update_judge_scores,
)
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
        teams = get_judge_dashboard_rows(judge_profile.id)
    except SQLAlchemyError as exc:
        current_app.logger.error("Judge dashboard load failed: %s", exc)
        flash("Unable to load teams for scoring.", "danger")
        teams = []

    return render_template(
        "judge/dashboard.html",
        judge=current_user,
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

    snapshot = get_judge_team_score_snapshot(judge_profile.id, team.id)
    score_values = snapshot["score_values"].copy()
    remarks = snapshot["remarks"]

    if request.method == "POST":
        raw_scores = {item["key"]: request.form.get(item["key"], "").strip() for item in CATEGORY_DEFINITIONS}
        remarks = request.form.get("remarks", "").strip()

        try:
            for item in CATEGORY_DEFINITIONS:
                score_text = raw_scores[item["key"]]
                if not score_text:
                    raise ValueError(f"{item['label']} is required.")

            save_or_update_judge_scores(judge_profile.id, team.id, raw_scores, remarks)
            flash("Scores saved successfully.", "success")

            if request.form.get("action") == "save_next":
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
        category_definitions=CATEGORY_DEFINITIONS,
        score_values=score_values,
        remarks=remarks,
        existing_snapshot=snapshot,
        preview_total=calculate_total_from_raw_scores(preview_raw_scores),
    )
