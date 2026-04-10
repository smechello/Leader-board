from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from models.user import User
from services.scoring_service import get_live_scoreboard_rows
from utils.auth import authenticate_admin


public_bp = Blueprint("public", __name__)


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
    )


@public_bp.get("/api/scoreboard")
def scoreboard_data():
    rows = get_live_scoreboard_rows()
    return jsonify(
        {
            "rows": rows,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@public_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _redirect_to_role_dashboard(getattr(current_user, "role", None))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "warning")
            return render_template("public/login.html")

        admin_user = authenticate_admin(username, password)
        if admin_user:
            login_user(admin_user)
            flash("Admin login successful.", "success")
            return redirect(url_for("admin.dashboard"))

        try:
            judge_user = User.query.filter_by(
                username=username,
                role="judge",
                is_active=True,
            ).first()
        except SQLAlchemyError as exc:
            current_app.logger.error("Judge login lookup failed: %s", exc)
            flash(
                "Judge authentication is unavailable until database schema is ready.",
                "danger",
            )
            return render_template("public/login.html"), 503

        if judge_user and check_password_hash(judge_user.password_hash, password):
            if judge_user.judge_profile and not judge_user.judge_profile.is_active:
                flash("This judge account is inactive.", "warning")
                return render_template("public/login.html")

            login_user(judge_user)
            flash("Judge login successful.", "success")
            return redirect(url_for("judge.dashboard"))

        flash("Invalid username or password.", "danger")

    return render_template("public/login.html")


@public_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("public.home"))
