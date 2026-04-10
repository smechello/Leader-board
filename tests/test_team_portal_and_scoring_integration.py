from datetime import datetime, timedelta, timezone
import uuid

from werkzeug.security import generate_password_hash

from models import db
from models.auth_access import TeamDirectLoginLink
from models.presence import JudgePresence
from models.score import SCORE_CATEGORIES, Score
from models.scoring import ScoringCategorySetting
from models.team import Project, Team, TeamMember
from models.user import Judge, User


def _unique_name(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _default_scoring_form_payload():
    return {
        "weight_innovation_originality": "30.00",
        "max_innovation_originality": "10.00",
        "weight_technical_implementation": "30.00",
        "max_technical_implementation": "10.00",
        "weight_business_value_impact": "25.00",
        "max_business_value_impact": "10.00",
        "weight_presentation_clarity": "15.00",
        "max_presentation_clarity": "10.00",
    }


def test_admin_can_update_scoring_settings(app, admin_client):
    updated_payload = {
        "weight_innovation_originality": "28.00",
        "max_innovation_originality": "12.00",
        "weight_technical_implementation": "32.00",
        "max_technical_implementation": "11.00",
        "weight_business_value_impact": "25.00",
        "max_business_value_impact": "9.00",
        "weight_presentation_clarity": "15.00",
        "max_presentation_clarity": "8.00",
    }

    try:
        response = admin_client.post(
            "/admin/options/scoring",
            data=updated_payload,
            follow_redirects=False,
        )
        assert response.status_code == 302

        with app.app_context():
            settings = {
                row.category: row
                for row in ScoringCategorySetting.query.all()
            }
            assert float(settings["innovation_originality"].weight_percent) == 28.0
            assert float(settings["innovation_originality"].max_score) == 12.0
            assert float(settings["technical_implementation"].weight_percent) == 32.0
            assert float(settings["technical_implementation"].max_score) == 11.0
    finally:
        admin_client.post(
            "/admin/options/scoring",
            data=_default_scoring_form_payload(),
            follow_redirects=False,
        )


def test_judge_score_input_is_clamped_to_configured_max(app, client):
    judge_username = _unique_name("judge_clamp")
    judge_password = "judgepass123"
    team_name = _unique_name("team_clamp")

    with app.app_context():
        settings = {
            row.category: float(row.max_score)
            for row in ScoringCategorySetting.query.all()
        }

        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash(judge_password),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Clamp", is_active=True)

        team = Team(team_name=team_name, process="General", theme="Clamp", is_active=True)
        team.project = Project(
            project_title="Clamp Project",
            problem_statement="Clamp Problem",
            project_summary="Clamp Summary",
        )

        db.session.add(judge_user)
        db.session.add(team)
        db.session.commit()

        team_id = team.id
        judge_id = judge_user.judge_profile.id
        judge_user_id = judge_user.id

    try:
        login_response = client.post(
            "/login",
            data={"username": judge_username, "password": judge_password},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        oversized_innovation = settings["innovation_originality"] + 5
        score_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": str(oversized_innovation),
                "technical_implementation": str(settings["technical_implementation"]),
                "business_value_impact": str(settings["business_value_impact"]),
                "presentation_clarity": str(settings["presentation_clarity"]),
                "remarks": "Clamp check",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert score_response.status_code == 302

        with app.app_context():
            row = Score.query.filter_by(judge_id=judge_id, team_id=team_id, category="innovation_originality").first()
            assert row is not None
            assert float(row.raw_score) == settings["innovation_originality"]
    finally:
        with app.app_context():
            user = User.query.filter_by(id=judge_user_id).first()
            team = Team.query.filter_by(id=team_id).first()
            if team:
                db.session.delete(team)
            if user:
                db.session.delete(user)
            db.session.commit()


def test_team_portal_and_judge_presence_flow(app, client, admin_client):
    judge_username = _unique_name("judge_presence")
    judge_password = "judgepass123"
    team_name = _unique_name("team_portal")
    team_login_id = _unique_name("team_login")
    team_password = "team-pass-123"

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash(judge_password),
            role="judge",
            is_active=True,
        )
        judge_profile = Judge(user=judge_user, display_name="Judge Presence", is_active=True)

        team = Team(
            team_name=team_name,
            sort_order=1,
            portal_login_id=team_login_id,
            portal_password_hash=generate_password_hash(team_password),
            process="General",
            theme="Portal",
            is_active=True,
        )
        team.project = Project(
            project_title="Portal Project",
            problem_statement="Portal Problem",
            project_summary="Portal Summary",
        )
        team.members.append(TeamMember(full_name="Member One", email=f"{_unique_name('member')}@internal.local"))

        db.session.add(judge_user)
        db.session.add(team)
        db.session.commit()

        for category in SCORE_CATEGORIES:
            db.session.add(
                Score(
                    judge_id=judge_profile.id,
                    team_id=team.id,
                    category=category,
                    raw_score=8,
                    weighted_score=20 if category != "presentation_clarity" else 12,
                    remarks="Good work",
                )
            )
        db.session.commit()

        direct_token = _unique_name("token")
        db.session.add(
            TeamDirectLoginLink(
                team_id=team.id,
                token=direct_token,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                created_by_admin="admin",
            )
        )
        db.session.commit()

        judge_user_id = judge_user.id
        judge_profile_id = judge_profile.id
        team_id = team.id

    judge_client = app.test_client()
    team_client = app.test_client()
    direct_team_client = app.test_client()

    try:
        login_response = judge_client.post(
            "/login",
            data={"username": judge_username, "password": judge_password},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        heartbeat_response = judge_client.post("/judge/presence/heartbeat")
        assert heartbeat_response.status_code == 200

        presence_response = admin_client.get("/admin/notifications/judge-presence")
        assert presence_response.status_code == 200
        assert presence_response.get_json()["online"].get(str(judge_profile_id)) is True

        team_login_response = team_client.post(
            "/team/login",
            data={"team_login_id": team_login_id, "password": team_password},
            follow_redirects=False,
        )
        assert team_login_response.status_code == 302

        team_portal_response = team_client.get("/team/portal")
        assert team_portal_response.status_code == 200
        assert "Judge Presence" in team_portal_response.get_data(as_text=True)
        assert "Member One" in team_portal_response.get_data(as_text=True)

        direct_link_response = direct_team_client.get(f"/team/direct-login/{direct_token}", follow_redirects=False)
        assert direct_link_response.status_code == 302

        direct_team_portal_response = direct_team_client.get("/team/portal")
        assert direct_team_portal_response.status_code == 200

        judge_client.get("/logout", follow_redirects=False)
        presence_after_logout = admin_client.get("/admin/notifications/judge-presence")
        assert presence_after_logout.status_code == 200
        assert presence_after_logout.get_json()["online"].get(str(judge_profile_id)) is False
    finally:
        with app.app_context():
            JudgePresence.query.filter_by(judge_id=judge_profile_id).delete(synchronize_session=False)
            user = User.query.filter_by(id=judge_user_id).first()
            team = Team.query.filter_by(id=team_id).first()
            if team:
                db.session.delete(team)
            if user:
                db.session.delete(user)
            db.session.commit()
