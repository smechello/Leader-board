from datetime import datetime, timezone
import secrets

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import or_
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
)
from models.user import Judge, User
from services.scoring_service import get_live_scoreboard_rows, get_scoreboard_tie_break_rule
from utils.auth import authenticate_admin


public_bp = Blueprint("public", __name__)
LOGIN_REQUEST_POLL_INTERVAL_MS = 4000


def _utcnow():
    return datetime.now(timezone.utc)


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
            or_(User.username == normalized_identifier, Judge.display_name == normalized_identifier),
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
    return render_template("public/home.html")


@public_bp.get("/scoreboard")
def scoreboard():
    rows = get_live_scoreboard_rows()
    return render_template(
        "public/scoreboard.html",
        rows=rows,
        refresh_interval_ms=5000,
        generated_at=datetime.now(timezone.utc),
        tie_break_rule=get_scoreboard_tie_break_rule(),
    )


@public_bp.get("/api/scoreboard")
def scoreboard_data():
    rows = get_live_scoreboard_rows()
    return jsonify(
        {
            "rows": rows,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tie_break_rule": get_scoreboard_tie_break_rule(),
        }
    )


@public_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _redirect_to_role_dashboard(getattr(current_user, "role", None))

    prefill_username = request.args.get("username", "").strip()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        prefill_username = username

        if not username or not password:
            flash("Name or username and password are required.", "warning")
            return render_template(
                "public/login.html",
                prefill_username=prefill_username,
                login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
            )

        admin_user = authenticate_admin(username, password)
        if admin_user:
            login_user(admin_user)
            flash("Admin login successful.", "success")
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
                flash("This judge account is inactive.", "warning")
                return render_template(
                    "public/login.html",
                    prefill_username=prefill_username,
                    login_request_poll_interval_ms=LOGIN_REQUEST_POLL_INTERVAL_MS,
                )

            login_user(judge_user)
            flash("Judge login successful.", "success")
            return redirect(url_for("judge.dashboard"))

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
        return jsonify({"error": "Name or username is required."}), 400

    try:
        judge_user = _find_active_judge_user(requested_login)
        if not judge_user or not judge_user.judge_profile:
            return jsonify({"error": "Judge account not found."}), 404

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
        return jsonify({"status": request_row.status, "redirect_url": url_for("judge.dashboard")})
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.error("Consume login request failed: %s", exc)
        return jsonify({"error": "Unable to complete direct login."}), 500


@public_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("public.home"))
