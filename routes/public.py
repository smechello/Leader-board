from datetime import datetime, timezone
import secrets
from threading import Lock
from time import monotonic

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash

from models import db
from models.auth_access import (
    LOGIN_REQUEST_STATUS_APPROVED,
    LOGIN_REQUEST_STATUS_CONSUMED,
    LOGIN_REQUEST_STATUS_EXPIRED,
    LOGIN_REQUEST_STATUS_PENDING,
    JudgeDirectLoginLink,
    JudgeLoginRequest,
    TeamDirectLoginLink,
)
from models.score import SCORE_CATEGORIES, Score
from models.team import Team, TeamMember
from models.user import Judge, User
from services.presence_service import mark_judge_offline, mark_judge_online
from services.scoring_config_service import CATEGORY_LABELS, get_category_definitions
from services.scoring_service import (
    SCOREBOARD_CACHE_TTL_SECONDS,
    get_cached_live_scoreboard_snapshot,
    get_scoreboard_tie_break_rule,
)
from utils.auth import authenticate_admin
from utils.team_auth import (
    authenticate_team,
    get_logged_in_team,
    login_team,
    logout_team,
    team_login_required,
)


public_bp = Blueprint("public", __name__)
LOGIN_REQUEST_POLL_INTERVAL_MS = 4000
SCOREBOARD_REFRESH_INTERVAL_MS = 5000

_scoreboard_html_cache_lock = Lock()
_scoreboard_html_cache = {}


def _utcnow():
    return datetime.now(timezone.utc)


def _scoreboard_html_cache_key():
    if current_user.is_authenticated:
        return f"auth:{getattr(current_user, 'role', 'user')}"
    if session.get("team_portal_team_id"):
        return "team_portal"
    return "anonymous"


def _has_pending_flash_messages():
    return bool(session.get("_flashes"))


def _parse_refresh_flag():
    value = (request.args.get("refresh") or "").strip().lower()
    return value in {"1", "true", "yes", "y"}


def _get_cached_scoreboard_html(cache_key):
    with _scoreboard_html_cache_lock:
        entry = _scoreboard_html_cache.get(cache_key)
        if not entry:
            return None

        age_seconds = monotonic() - float(entry.get("created_at_monotonic") or 0.0)
        if age_seconds >= SCOREBOARD_CACHE_TTL_SECONDS:
            _scoreboard_html_cache.pop(cache_key, None)
            return None

        return entry.get("html")


def _set_cached_scoreboard_html(cache_key, rendered_html):
    with _scoreboard_html_cache_lock:
        _scoreboard_html_cache[cache_key] = {
            "created_at_monotonic": monotonic(),
            "html": rendered_html,
        }


def _find_active_judge_user(identifier):
    normalized_identifier = (identifier or "").strip()
    if not normalized_identifier:
        return None

    return (
        User.query.join(Judge, Judge.user_id == User.id)
        .filter(
            User.role == "judge",
            User.is_active.is_(True),
            Judge.is_active.is_(True),
            User.username == normalized_identifier,
        )
        .first()
    )


def _redirect_to_role_dashboard(role):
    if role == "admin":
        return redirect(url_for("admin.dashboard"))
    if role == "judge":
        return redirect(url_for("judge.dashboard"))
    return redirect(url_for("public.home"))


@public_bp.get("/")
def home():
    if current_user.is_authenticated and getattr(current_user, "role", None) == "judge":
        return redirect(url_for("judge.dashboard"))

    return render_template("public/home.html")


@public_bp.get("/scoreboard")
def scoreboard():
    force_refresh = _parse_refresh_flag()
    can_use_html_cache = (
        not current_app.config.get("TESTING")
        and not force_refresh
        and not _has_pending_flash_messages()
    )

    cache_key = _scoreboard_html_cache_key()
    if can_use_html_cache:
        cached_html = _get_cached_scoreboard_html(cache_key)
        if cached_html is not None:
            response = current_app.response_class(cached_html, mimetype="text/html")
            response.headers["X-Scoreboard-HTML-Cache"] = "HIT"
            response.headers["X-Scoreboard-Data-Cache"] = "HIT"
            return response

    snapshot = get_cached_live_scoreboard_snapshot(
        max_age_seconds=SCOREBOARD_CACHE_TTL_SECONDS,
        force_refresh=force_refresh,
    )

    rendered_html = render_template(
        "public/scoreboard.html",
        rows=snapshot["rows"],
        refresh_interval_ms=SCOREBOARD_REFRESH_INTERVAL_MS,
        generated_at=snapshot["generated_at"],
        tie_break_rule=get_scoreboard_tie_break_rule(),
    )

    if can_use_html_cache:
        _set_cached_scoreboard_html(cache_key, rendered_html)

    response = current_app.response_class(rendered_html, mimetype="text/html")
    response.headers["X-Scoreboard-HTML-Cache"] = "MISS" if can_use_html_cache else "BYPASS"
    response.headers["X-Scoreboard-Data-Cache"] = "HIT" if snapshot["cache_hit"] else "MISS"
    return response


@public_bp.get("/api/scoreboard")
def scoreboard_data():
    force_refresh = _parse_refresh_flag()
    snapshot = get_cached_live_scoreboard_snapshot(
        max_age_seconds=SCOREBOARD_CACHE_TTL_SECONDS,
        force_refresh=force_refresh,
    )

    response = jsonify(
        {
            "rows": snapshot["rows"],
            "generated_at": (
                snapshot["generated_at"].isoformat()
                if snapshot.get("generated_at")
                else datetime.now(timezone.utc).isoformat()
            ),
            "tie_break_rule": get_scoreboard_tie_break_rule(),
        }
    )
    response.headers["X-Scoreboard-Data-Cache"] = "HIT" if snapshot["cache_hit"] else "MISS"
    return response


@public_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _redirect_to_role_dashboard(getattr(current_user, "role", None))

    existing_team = get_logged_in_team()
    if existing_team:
        return redirect(url_for("public.team_portal"))

    prefill_username = request.args.get("username", "").strip() or request.args.get("team_id", "").strip()

    if request.method == "POST":
        username = (request.form.get("username") or request.form.get("team_login_id") or "").strip()
        password = request.form.get("password", "")
        prefill_username = username

        if not username or not password:
            flash("Username or Team ID and password are required.", "warning")
            return render_template(
                "public/login.html",
                prefill_username=prefill_username,
                login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
            )

        admin_user = authenticate_admin(username, password)
        if admin_user:
            login_user(admin_user)
            flash("Login successful.", "success")
            return redirect(url_for("admin.dashboard"))

        try:
            judge_user = _find_active_judge_user(username)
        except SQLAlchemyError as exc:
            current_app.logger.error("Judge login lookup failed: %s", exc)
            flash(
                "Judge authentication is unavailable until database schema is ready.",
                "danger",
            )
            return (
                render_template(
                    "public/login.html",
                    prefill_username=prefill_username,
                    login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
                ),
                503,
            )

        if judge_user and check_password_hash(judge_user.password_hash, password):
            if judge_user.judge_profile and not judge_user.judge_profile.is_active:
                flash("This account is inactive.", "warning")
                return render_template(
                    "public/login.html",
                    prefill_username=prefill_username,
                    login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
                )

            login_user(judge_user)
            if judge_user.judge_profile:
                mark_judge_online(judge_user.judge_profile.id)
            flash("Login successful.", "success")
            return redirect(url_for("judge.dashboard"))

        team = authenticate_team(username, password)
        if team:
            login_team(team)
            flash("Login successful.", "success")
            return redirect(url_for("public.team_portal"))

        flash("Invalid username or password.", "danger")

    return render_template(
        "public/login.html",
        prefill_username=prefill_username,
        login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
    )


@public_bp.route("/judge/direct-login/<token>", methods=["GET", "POST"])
def judge_direct_login(token):
    try:
        direct_link = (
            JudgeDirectLoginLink.query.options(
                joinedload(JudgeDirectLoginLink.judge).joinedload(Judge.user)
            )
            .filter_by(token=token)
            .first()
        )
    except SQLAlchemyError as exc:
        current_app.logger.error("Direct login link lookup failed: %s", exc)
        flash("Direct login is temporarily unavailable.", "danger")
        return redirect(url_for("public.login"))

    if not direct_link:
        flash("This direct login link is invalid.", "warning")
        return redirect(url_for("public.login"))

    judge_profile = direct_link.judge
    judge_user = judge_profile.user if judge_profile else None
    username_hint = judge_user.username if judge_user else ""
    now_utc = _utcnow()

    if direct_link.revoked_at is not None:
        flash("This direct login link is no longer active.", "warning")
        return redirect(url_for("public.login", username=username_hint))

    if direct_link.expires_at <= now_utc:
        flash("This direct login link has expired. Enter password or request direct login.", "warning")
        return redirect(url_for("public.login", username=username_hint))

    if not judge_profile or not judge_user or not judge_profile.is_active or not judge_user.is_active:
        flash("Judge account is inactive. Contact admin.", "warning")
        return redirect(url_for("public.login", username=username_hint))

    if request.method == "POST":
        decision = request.form.get("decision", "").strip().lower()

        if decision == "yes":
            try:
                direct_link.last_used_at = now_utc
                db.session.commit()
            except SQLAlchemyError as exc:
                db.session.rollback()
                current_app.logger.error("Direct login link update failed: %s", exc)
                flash("Unable to complete direct login right now.", "danger")
                return redirect(url_for("public.login", username=username_hint))

            login_user(judge_user)
            mark_judge_online(judge_profile.id)
            flash("Direct login successful.", "success")
            return redirect(url_for("judge.dashboard"))

        if decision == "no":
            try:
                direct_link.revoked_at = now_utc
                direct_link.revoke_reason = "judge_declined_confirmation"
                db.session.commit()
            except SQLAlchemyError as exc:
                db.session.rollback()
                current_app.logger.error("Direct login revoke on decline failed: %s", exc)

            flash("Link closed. Please login using password.", "info")
            return redirect(url_for("public.login", username=username_hint))

        flash("Please choose Yes or No to continue.", "warning")

    return render_template(
        "public/direct_login_confirm.html",
        judge_name=judge_profile.display_name,
        username_hint=username_hint,
        expires_at=direct_link.expires_at,
    )


@public_bp.post("/login/request-access")
def request_login_access():
    if current_user.is_authenticated:
        return jsonify({"error": "Already authenticated."}), 400

    request_data = request.get_json(silent=True) or request.form
    requested_login = (request_data.get("username") or "").strip()

    if not requested_login:
        return jsonify({"error": "Username is required."}), 400

    try:
        judge_user = _find_active_judge_user(requested_login)
        if not judge_user or not judge_user.judge_profile:
            return jsonify({"error": "Account not found."}), 404

        judge_profile = judge_user.judge_profile
        pending_request = (
            JudgeLoginRequest.query.filter_by(
                judge_id=judge_profile.id,
                status=LOGIN_REQUEST_STATUS_PENDING,
            )
            .order_by(JudgeLoginRequest.created_at.desc())
            .first()
        )

        if pending_request:
            request_row = pending_request
        else:
            request_row = JudgeLoginRequest(
                judge_id=judge_profile.id,
                request_key=secrets.token_urlsafe(24),
                requested_login=requested_login,
                status=LOGIN_REQUEST_STATUS_PENDING,
            )
            db.session.add(request_row)
            db.session.commit()

        return jsonify(
            {
                "request_id": request_row.id,
                "request_key": request_row.request_key,
                "status": request_row.status,
                "poll_interval_ms": LOGIN_REQUEST_POLL_INTERVAL_MS,
            }
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Login access request failed: %s", exc)
        return jsonify({"error": "Unable to raise login request right now."}), 500


@public_bp.get("/login/request-status/<int:request_id>")
def login_request_status(request_id):
    request_key = request.args.get("key", "").strip()
    if not request_key:
        return jsonify({"error": "Request key is required."}), 400

    try:
        request_row = JudgeLoginRequest.query.filter_by(id=request_id, request_key=request_key).first()
        if not request_row:
            return jsonify({"error": "Login request not found."}), 404

        now_utc = _utcnow()
        if (
            request_row.status == LOGIN_REQUEST_STATUS_APPROVED
            and request_row.approval_expires_at is not None
            and request_row.approval_expires_at <= now_utc
            and request_row.consumed_at is None
        ):
            request_row.status = LOGIN_REQUEST_STATUS_EXPIRED
            request_row.decided_at = now_utc
            db.session.commit()

        return jsonify(
            {
                "status": request_row.status,
                "approval_expires_at": (
                    request_row.approval_expires_at.isoformat() if request_row.approval_expires_at else None
                ),
            }
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Login request status check failed: %s", exc)
        return jsonify({"error": "Unable to check request status."}), 500


@public_bp.post("/login/request-consume")
def consume_login_request():
    request_data = request.get_json(silent=True) or request.form

    request_id_raw = request_data.get("request_id")
    request_key = (request_data.get("request_key") or "").strip()

    try:
        request_id = int(request_id_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid request id."}), 400

    if not request_key:
        return jsonify({"error": "Request key is required."}), 400

    try:
        request_row = (
            JudgeLoginRequest.query.options(joinedload(JudgeLoginRequest.judge).joinedload(Judge.user))
            .filter_by(id=request_id, request_key=request_key)
            .first()
        )
        if not request_row:
            return jsonify({"error": "Login request not found."}), 404

        now_utc = _utcnow()
        if request_row.status != LOGIN_REQUEST_STATUS_APPROVED:
            return jsonify({"error": "Login request is not approved.", "status": request_row.status}), 409

        if request_row.approval_expires_at and request_row.approval_expires_at <= now_utc:
            request_row.status = LOGIN_REQUEST_STATUS_EXPIRED
            request_row.decided_at = now_utc
            db.session.commit()
            return jsonify({"error": "Approval expired.", "status": request_row.status}), 409

        judge_profile = request_row.judge
        judge_user = judge_profile.user if judge_profile else None
        if not judge_profile or not judge_user or not judge_profile.is_active or not judge_user.is_active:
            return jsonify({"error": "Judge account is inactive."}), 409

        request_row.status = LOGIN_REQUEST_STATUS_CONSUMED
        request_row.consumed_at = now_utc
        db.session.commit()

        login_user(judge_user)
        mark_judge_online(judge_profile.id)
        return jsonify({"status": request_row.status, "redirect_url": url_for("judge.dashboard")})
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Consume login request failed: %s", exc)
        return jsonify({"error": "Unable to complete direct login."}), 500


@public_bp.route("/team/login", methods=["GET", "POST"])
def team_login():
    # Backward-compatible alias: use the single login portal.
    if request.method == "POST":
        login_id = (request.form.get("team_login_id") or request.form.get("username") or "").strip()
        password = request.form.get("password", "")

        if login_id and password:
            team = authenticate_team(login_id, password)
            if team:
                login_team(team)
                flash("Login successful.", "success")
                return redirect(url_for("public.team_portal"))

        flash("Invalid username or password.", "danger")
        return redirect(url_for("public.login", username=login_id))

    prefill_login_id = request.args.get("team_id", "").strip()
    return redirect(url_for("public.login", username=prefill_login_id))


@public_bp.get("/team/direct-login/<token>")
def team_direct_login(token):
    try:
        direct_link = TeamDirectLoginLink.query.filter_by(token=token).first()
    except SQLAlchemyError as exc:
        current_app.logger.error("Team direct login link lookup failed: %s", exc)
        flash("Team direct login is temporarily unavailable.", "danger")
        return redirect(url_for("public.login"))

    if not direct_link:
        flash("This team link is invalid.", "warning")
        return redirect(url_for("public.login"))

    team = Team.query.filter_by(id=direct_link.team_id).first()
    team_id_hint = team.portal_login_id if team else ""
    now_utc = _utcnow()

    if direct_link.revoked_at is not None:
        flash("This team link is no longer active.", "warning")
        return redirect(url_for("public.login", username=team_id_hint))

    if direct_link.expires_at <= now_utc:
        flash("This team link has expired. Please login with Team ID and password.", "warning")
        return redirect(url_for("public.login", username=team_id_hint))

    if not team or not team.portal_login_id or not team.portal_password_hash:
        flash("Team account is not ready for login.", "warning")
        return redirect(url_for("public.login", username=team_id_hint))

    try:
        direct_link.last_used_at = now_utc
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()

    login_team(team)
    flash("Team direct login successful.", "success")
    return redirect(url_for("public.team_portal"))


@public_bp.get("/team/portal")
@team_login_required
def team_portal():
    team = get_logged_in_team()
    if not team:
        return redirect(url_for("public.login"))

    members = TeamMember.query.filter_by(team_id=team.id).order_by(TeamMember.id.asc()).all()

    judge_rows = (
        db.session.query(Score, Judge, User)
        .join(Judge, Judge.id == Score.judge_id)
        .join(User, User.id == Judge.user_id)
        .filter(Score.team_id == team.id)
        .order_by(Judge.display_name.asc(), Score.category.asc())
        .all()
    )

    by_judge = {}
    for score_row, judge, user in judge_rows:
        judge_entry = by_judge.get(judge.id)
        if not judge_entry:
            judge_entry = {
                "judge_name": judge.display_name,
                "judge_login_key": user.username,
                "categories": {category: None for category in SCORE_CATEGORIES},
                "remarks": "",
                "total_weighted": 0.0,
            }
            by_judge[judge.id] = judge_entry

        judge_entry["categories"][score_row.category] = float(score_row.raw_score)
        judge_entry["total_weighted"] += float(score_row.weighted_score or 0)
        if score_row.remarks and not judge_entry["remarks"]:
            judge_entry["remarks"] = score_row.remarks

    judge_scores = sorted(by_judge.values(), key=lambda item: item["judge_name"].lower())
    for item in judge_scores:
        item["total_weighted"] = round(item["total_weighted"], 2)

    return render_template(
        "public/team_portal.html",
        team=team,
        members=members,
        category_keys=SCORE_CATEGORIES,
        category_labels=CATEGORY_LABELS,
        scoring_definitions=get_category_definitions(),
        judge_scores=judge_scores,
    )


@public_bp.get("/team/logout")
def team_logout():
    logout_team()
    flash("Logged out successfully.", "info")
    return redirect(url_for("public.login"))


@public_bp.get("/logout")
@login_required
def logout():
    if getattr(current_user, "role", None) == "judge" and getattr(current_user, "judge_profile", None):
        try:
            mark_judge_offline(current_user.judge_profile.id)
        except SQLAlchemyError:
            db.session.rollback()

    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("public.home"))
