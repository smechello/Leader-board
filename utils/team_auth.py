from functools import wraps

from flask import flash, redirect, session, url_for
from werkzeug.security import check_password_hash

from models.team import Team


TEAM_SESSION_KEY = "team_portal_team_id"


def get_logged_in_team():
    team_id = session.get(TEAM_SESSION_KEY)
    if not team_id:
        return None

    team = Team.query.filter_by(id=team_id, is_active=True).first()
    if not team:
        session.pop(TEAM_SESSION_KEY, None)
        return None

    return team


def login_team(team):
    session[TEAM_SESSION_KEY] = team.id


def logout_team():
    session.pop(TEAM_SESSION_KEY, None)


def authenticate_team(login_id, password):
    team = Team.query.filter_by(portal_login_id=(login_id or "").strip(), is_active=True).first()
    if not team or not team.portal_password_hash:
        return None

    if not check_password_hash(team.portal_password_hash, password or ""):
        return None

    return team


def team_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        team = get_logged_in_team()
        if not team:
            flash("Team login is required.", "warning")
            return redirect(url_for("public.team_login"))

        return view_func(*args, **kwargs)

    return wrapped
