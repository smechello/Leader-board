import time
import uuid

from werkzeug.security import generate_password_hash

from models import db
from models.audit import AuditLog
from models.score import SCORE_CATEGORIES, Score
from models.team import Project, Team, TeamMember
from models.user import Judge, User


def _unique_name(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_access_control_and_not_found(client):
    admin_redirect = client.get("/admin/dashboard", follow_redirects=False)
    assert admin_redirect.status_code == 302
    assert "/login" in (admin_redirect.headers.get("Location") or "")

    not_found_response = client.get("/route-that-does-not-exist")
    assert not_found_response.status_code == 404


def test_admin_team_and_member_crud_flow(app, admin_client):
    team_name = _unique_name("team_step9")

    response_create = admin_client.post(
        "/admin/teams/new",
        data={
            "team_name": team_name,
            "process": "Prototype",
            "theme": "AI",
            "project_title": "Step 9 Project",
            "problem_statement": "Solve x",
            "project_summary": "Summary x",
            "repository_url": "https://example.com/repo",
            "demo_url": "https://example.com/demo",
            "notes_url": "https://example.com/notes",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response_create.status_code == 302

    with app.app_context():
        team = Team.query.filter_by(team_name=team_name).first()
        assert team is not None
        team_id = team.id

    response_edit = admin_client.post(
        f"/admin/teams/{team_id}/edit",
        data={
            "team_name": team_name,
            "process": "Final",
            "theme": "AI Updated",
            "project_title": "Step 9 Project Updated",
            "problem_statement": "Solve y",
            "project_summary": "Summary y",
            "repository_url": "https://example.com/repo2",
            "demo_url": "https://example.com/demo2",
            "notes_url": "https://example.com/notes2",
            "is_active": "on",
        },
        follow_redirects=False,
    )
    assert response_edit.status_code == 302

    response_add_member = admin_client.post(
        f"/admin/teams/{team_id}/members",
        data={
            "full_name": "Member Step9",
            "email": f"member_{team_name}@example.com",
            "phone": "",
            "department_or_class": "CSE",
        },
        follow_redirects=False,
    )
    assert response_add_member.status_code == 302

    with app.app_context():
        team = Team.query.filter_by(id=team_id).first()
        assert team is not None
        assert team.theme == "AI Updated"

        member = TeamMember.query.filter_by(team_id=team_id).first()
        assert member is not None
        member_id = member.id

    response_delete_member = admin_client.post(
        f"/admin/teams/{team_id}/members/{member_id}/delete",
        follow_redirects=False,
    )
    assert response_delete_member.status_code == 302

    response_deactivate = admin_client.post(
        f"/admin/teams/{team_id}/toggle-active",
        follow_redirects=False,
    )
    assert response_deactivate.status_code == 302

    with app.app_context():
        team = Team.query.filter_by(id=team_id).first()
        assert team is not None
        assert team.is_active is False
        assert TeamMember.query.filter_by(team_id=team_id).count() == 0

    response_delete_team = admin_client.post(
        f"/admin/teams/{team_id}/delete",
        follow_redirects=False,
    )
    assert response_delete_team.status_code == 302

    with app.app_context():
        assert Team.query.filter_by(id=team_id).first() is None


def test_judge_score_edit_and_lock_flow(app, client):
    judge_username = _unique_name("judge_step9")
    judge_password = "judgepass123"
    team_name = _unique_name("team_lock_step9")

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash(judge_password),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Step 9", is_active=True)

        team = Team(team_name=team_name, theme="LockTest", is_active=True)
        team.project = Project(
            project_title="Lock Project",
            problem_statement="Lock Problem",
            project_summary="Lock Summary",
        )

        db.session.add(judge_user)
        db.session.add(team)
        db.session.commit()

        judge_user_id = judge_user.id
        judge_id = judge_user.judge_profile.id
        team_id = team.id

    try:
        login_response = client.post(
            "/login",
            data={"username": judge_username, "password": judge_password},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        save_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": "8",
                "technical_implementation": "7",
                "business_value_impact": "9",
                "presentation_clarity": "6",
                "remarks": "Initial",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert save_response.status_code == 302

        edit_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": "9",
                "technical_implementation": "7",
                "business_value_impact": "9",
                "presentation_clarity": "6",
                "remarks": "Edited",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert edit_response.status_code == 302

        lock_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": "9",
                "technical_implementation": "7",
                "business_value_impact": "9",
                "presentation_clarity": "6",
                "remarks": "Locked",
                "action": "save_lock",
            },
            follow_redirects=False,
        )
        assert lock_response.status_code == 302

        blocked_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": "5",
                "technical_implementation": "5",
                "business_value_impact": "5",
                "presentation_clarity": "5",
                "remarks": "Should be blocked",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert blocked_response.status_code == 302

        with app.app_context():
            rows = Score.query.filter_by(judge_id=judge_id, team_id=team_id).all()
            assert len(rows) == 4
            assert all(row.is_locked for row in rows)
            total = round(sum(float(row.weighted_score or 0) for row in rows), 2)
            assert total == 79.5

            audit_count = AuditLog.query.filter_by(actor_user_id=judge_user_id).count()
            assert audit_count > 0
    finally:
        with app.app_context():
            AuditLog.query.filter_by(actor_user_id=judge_user_id).delete(synchronize_session=False)
            user = User.query.filter_by(id=judge_user_id).first()
            team = Team.query.filter_by(id=team_id).first()
            if team:
                db.session.delete(team)
            if user:
                db.session.delete(user)
            db.session.commit()


def test_public_scoreboard_ranking_updates_without_login(app):
    judge_username = _unique_name("judge_public_step9")
    team_high = _unique_name("team_public_high")
    team_low = _unique_name("team_public_low")

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash("judgepass123"),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Public", is_active=True)

        high_team = Team(team_name=team_high, theme="Theme A", is_active=True)
        high_team.project = Project(
            project_title="High Project",
            problem_statement="High Problem",
            project_summary="High Summary",
        )

        low_team = Team(team_name=team_low, theme="Theme B", is_active=True)
        low_team.project = Project(
            project_title="Low Project",
            problem_statement="Low Problem",
            project_summary="Low Summary",
        )

        db.session.add_all([judge_user, high_team, low_team])
        db.session.commit()

        judge_id = judge_user.judge_profile.id
        high_team_id = high_team.id
        low_team_id = low_team.id

        for category in SCORE_CATEGORIES:
            db.session.add(Score(judge_id=judge_id, team_id=high_team_id, category=category, raw_score=9, remarks="high"))
            db.session.add(Score(judge_id=judge_id, team_id=low_team_id, category=category, raw_score=4, remarks="low"))
        db.session.commit()

    try:
        client = app.test_client()
        page_response = client.get("/scoreboard")
        assert page_response.status_code == 200

        api_before = client.get("/api/scoreboard")
        assert api_before.status_code == 200
        rows_before = api_before.get_json()["rows"]

        index_high_before = next(i for i, row in enumerate(rows_before) if row["team_name"] == team_high)
        index_low_before = next(i for i, row in enumerate(rows_before) if row["team_name"] == team_low)
        assert index_high_before < index_low_before

        with app.app_context():
            high_rows = Score.query.filter_by(judge_id=judge_id, team_id=high_team_id).all()
            low_rows = Score.query.filter_by(judge_id=judge_id, team_id=low_team_id).all()
            for row in high_rows:
                row.raw_score = 3
            for row in low_rows:
                row.raw_score = 10
            db.session.commit()

        api_after = client.get("/api/scoreboard")
        assert api_after.status_code == 200
        payload_after = api_after.get_json()
        rows_after = payload_after["rows"]
        assert "tie_break_rule" in payload_after

        index_high_after = next(i for i, row in enumerate(rows_after) if row["team_name"] == team_high)
        index_low_after = next(i for i, row in enumerate(rows_after) if row["team_name"] == team_low)
        assert index_high_after > index_low_after
    finally:
        with app.app_context():
            user = User.query.filter_by(username=judge_username).first()
            high_team = Team.query.filter_by(team_name=team_high).first()
            low_team = Team.query.filter_by(team_name=team_low).first()
            if high_team:
                db.session.delete(high_team)
            if low_team:
                db.session.delete(low_team)
            if user:
                AuditLog.query.filter_by(actor_user_id=user.id).delete(synchronize_session=False)
                db.session.delete(user)
            db.session.commit()
