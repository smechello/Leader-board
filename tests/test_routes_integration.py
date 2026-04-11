from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import uuid

import pytest
from werkzeug.security import generate_password_hash

from models import db
from models.audit import AuditLog
from models.auth_access import JudgeDirectLoginLink, JudgeLoginRequest, TeamDirectLoginLink
from models.options import ProcessOption, SystemSetting, ThemeOption
from models.presence import JudgePresence
from models.score import SCORE_CATEGORIES, Score
from models.scoring import ScoringCategorySetting
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


def test_home_redirects_authenticated_judge_to_dashboard(app, client):
    judge_username = _unique_name("judge_home")
    judge_password = "judgepass123"

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash(judge_password),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Home", is_active=True)
        db.session.add(judge_user)
        db.session.commit()
        judge_user_id = judge_user.id

    try:
        login_response = client.post(
            "/login",
            data={"username": judge_username, "password": judge_password},
            follow_redirects=False,
        )
        assert login_response.status_code == 302

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/judge/dashboard" in (response.headers.get("Location") or "")
    finally:
        with app.app_context():
            user = User.query.filter_by(id=judge_user_id).first()
            if user:
                db.session.delete(user)
                db.session.commit()


def test_admin_kill_switch_wipes_database_and_restores_defaults(app, admin_client):
    if os.getenv("RUN_DESTRUCTIVE_TESTS") != "1":
        pytest.skip("Destructive kill-switch test skipped by default. Set RUN_DESTRUCTIVE_TESTS=1 to enable.")

    admin_password = app.config.get("ADMIN_PASSWORD")
    if not admin_password:
        pytest.skip("ADMIN_PASSWORD is required to validate kill switch flow.")

    judge_username = _unique_name("judge_kill_switch")
    team_name = _unique_name("team_kill_switch")

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash("judgepass123"),
            role="judge",
            is_active=True,
        )
        judge_profile = Judge(user=judge_user, display_name="Judge Kill Switch", is_active=True)

        team = Team(team_name=team_name, process="General", theme="ResetTheme", is_active=True)
        team.project = Project(
            project_title="Kill Switch Project",
            problem_statement="Reset everything",
            project_summary="Reset summary",
        )
        team.members.append(
            TeamMember(
                full_name="Kill Switch Member",
                email=f"member_{team_name}@example.com",
            )
        )

        db.session.add(ProcessOption(name=_unique_name("proc")))
        db.session.add(ThemeOption(name=_unique_name("theme")))
        db.session.add(judge_user)
        db.session.add(team)
        db.session.flush()

        db.session.add(
            Score(
                judge_id=judge_profile.id,
                team_id=team.id,
                category="innovation_originality",
                raw_score=9,
                remarks="seed",
            )
        )
        db.session.add(
            JudgePresence(
                judge_id=judge_profile.id,
                is_online=True,
                last_seen_at=datetime.now(timezone.utc),
            )
        )
        db.session.add(
            JudgeDirectLoginLink(
                judge_id=judge_profile.id,
                token=_unique_name("judge_link"),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                created_by_admin="admin",
            )
        )
        db.session.add(
            JudgeLoginRequest(
                judge_id=judge_profile.id,
                request_key=_unique_name("request"),
                requested_login=judge_username,
                status="pending",
            )
        )
        db.session.add(
            TeamDirectLoginLink(
                team_id=team.id,
                token=_unique_name("team_link"),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                created_by_admin="admin",
            )
        )

        # Change one scoring setting so the kill switch must restore defaults.
        setting = ScoringCategorySetting.query.filter_by(category="innovation_originality").first()
        if setting:
            setting.weight_percent = 20
            setting.max_score = 15

        db.session.commit()

        assert User.query.count() > 0
        assert Team.query.count() > 0
        assert Score.query.count() > 0

    response = admin_client.post(
        "/admin/kill-switch/wipe-database",
        data={"admin_password": admin_password},
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        assert User.query.count() == 0
        assert Judge.query.count() == 0
        assert Team.query.count() == 0
        assert TeamMember.query.count() == 0
        assert Project.query.count() == 0
        assert Score.query.count() == 0
        assert JudgePresence.query.count() == 0
        assert JudgeDirectLoginLink.query.count() == 0
        assert JudgeLoginRequest.query.count() == 0
        assert TeamDirectLoginLink.query.count() == 0
        assert AuditLog.query.count() == 0

        process_names = {row.name for row in ProcessOption.query.all()}
        theme_names = {row.name for row in ThemeOption.query.all()}
        assert "General" in process_names
        assert "General" in theme_names

        settings = {
            row.category: (float(row.weight_percent), float(row.max_score))
            for row in ScoringCategorySetting.query.all()
        }
        assert len(settings) == 4
        assert settings["innovation_originality"] == (30.0, 10.0)
        assert settings["technical_implementation"] == (30.0, 10.0)
        assert settings["business_value_impact"] == (25.0, 10.0)
        assert settings["presentation_clarity"] == (15.0, 10.0)


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

    with app.app_context():
        team = Team.query.filter_by(id=team_id).first()
        assert team is not None
        assert TeamMember.query.filter_by(team_id=team_id).count() == 0

    response_delete_team = admin_client.post(
        f"/admin/teams/{team_id}/delete",
        follow_redirects=False,
    )
    assert response_delete_team.status_code == 302

    with app.app_context():
        assert Team.query.filter_by(id=team_id).first() is None


def test_admin_team_reorder_flow(app, admin_client):
    team_names = [_unique_name("team_order_a"), _unique_name("team_order_b"), _unique_name("team_order_c")]

    with app.app_context():
        created_ids = []
        for index, team_name in enumerate(team_names, start=1):
            team = Team(
                team_name=team_name,
                sort_order=index,
                process="General",
                theme="OrderTest",
                is_active=True,
            )
            team.project = Project(
                project_title=f"Project {index}",
                problem_statement="Ordering test",
                project_summary="Ordering summary",
            )
            db.session.add(team)
        db.session.commit()

        created_rows = Team.query.filter(Team.team_name.in_(team_names)).order_by(Team.sort_order.asc(), Team.id.asc()).all()
        created_ids = [row.id for row in created_rows]

    try:
        with app.app_context():
            all_team_ids = [row.id for row in Team.query.order_by(Team.sort_order.asc(), Team.id.asc()).all()]

        reordered_created_ids = list(reversed(created_ids))
        remaining_ids = [team_id for team_id in all_team_ids if team_id not in set(created_ids)]
        reordered_ids = reordered_created_ids + remaining_ids

        reorder_response = admin_client.post(
            "/admin/teams/reorder",
            json={"team_ids": reordered_ids},
            follow_redirects=False,
        )
        assert reorder_response.status_code == 200
        assert reorder_response.get_json().get("ok") is True

        with app.app_context():
            ordered_rows = Team.query.filter(Team.id.in_(created_ids)).order_by(Team.sort_order.asc(), Team.id.asc()).all()
            assert [row.id for row in ordered_rows] == reordered_created_ids
    finally:
        with app.app_context():
            Team.query.filter(Team.id.in_(created_ids)).delete(synchronize_session=False)
            db.session.commit()


def test_admin_presentation_control_flow(app, admin_client):
    team_one_name = _unique_name("team_present_a")
    team_two_name = _unique_name("team_present_b")

    with app.app_context():
        team_one = Team(
            team_name=team_one_name,
            sort_order=-1000,
            process="General",
            theme="Presentation",
            is_active=True,
            presentation_completed=False,
        )
        team_one.project = Project(
            project_title="Presentation Project A",
            problem_statement="Statement A",
            project_summary="Summary A",
        )
        team_one.members.append(
            TeamMember(
                full_name="Presenter A",
                email=f"presenter_a_{uuid.uuid4().hex[:6]}@example.com",
            )
        )

        team_two = Team(
            team_name=team_two_name,
            sort_order=-999,
            process="General",
            theme="Presentation",
            is_active=True,
            presentation_completed=False,
        )
        team_two.project = Project(
            project_title="Presentation Project B",
            problem_statement="Statement B",
            project_summary="Summary B",
        )
        team_two.members.append(
            TeamMember(
                full_name="Presenter B",
                email=f"presenter_b_{uuid.uuid4().hex[:6]}@example.com",
            )
        )

        db.session.add(team_one)
        db.session.add(team_two)
        db.session.commit()

        team_one_id = team_one.id
        team_two_id = team_two.id

    try:
        timer_state_response = admin_client.get(
            "/admin/presentation/timer/state",
            follow_redirects=False,
        )
        assert timer_state_response.status_code == 200
        timer_state_payload = timer_state_response.get_json()
        assert timer_state_payload.get("ok") is True

        timer_start_response = admin_client.post(
            "/admin/presentation/timer/control",
            json={"action": "start"},
            follow_redirects=False,
        )
        assert timer_start_response.status_code == 200
        assert timer_start_response.get_json().get("running") is True

        timer_pause_response = admin_client.post(
            "/admin/presentation/timer/control",
            json={"action": "pause"},
            follow_redirects=False,
        )
        assert timer_pause_response.status_code == 200
        assert timer_pause_response.get_json().get("running") is False

        page_response = admin_client.get(
            f"/admin/presentation?team_id={team_one_id}",
            follow_redirects=False,
        )
        assert page_response.status_code == 200
        page_text = page_response.get_data(as_text=True)
        assert "Presentation Control" in page_text
        assert team_one_name in page_text
        assert team_two_name in page_text
        assert "Presenter A" in page_text
        assert "Presenter B" in page_text

        complete_response = admin_client.post(
            f"/admin/presentation/{team_one_id}/complete",
            follow_redirects=False,
        )
        assert complete_response.status_code == 302

        with app.app_context():
            refreshed_team_one = Team.query.filter_by(id=team_one_id).first()
            assert refreshed_team_one is not None
            assert refreshed_team_one.presentation_completed is True
            assert refreshed_team_one.presentation_completed_at is not None
            assert refreshed_team_one.presentation_elapsed_seconds is not None

        reopen_response = admin_client.post(
            f"/admin/presentation/{team_one_id}/reopen",
            follow_redirects=False,
        )
        assert reopen_response.status_code == 302

        with app.app_context():
            reopened_team_one = Team.query.filter_by(id=team_one_id).first()
            assert reopened_team_one is not None
            assert reopened_team_one.presentation_completed is False
            assert reopened_team_one.presentation_completed_at is None
            assert reopened_team_one.presentation_elapsed_seconds is None

        admin_client.post(
            f"/admin/presentation/{team_one_id}/complete",
            follow_redirects=False,
        )
        admin_client.post(
            f"/admin/presentation/{team_two_id}/complete",
            follow_redirects=False,
        )

        reset_response = admin_client.post(
            "/admin/presentation/reset",
            follow_redirects=False,
        )
        assert reset_response.status_code == 302

        with app.app_context():
            refreshed_team_one = Team.query.filter_by(id=team_one_id).first()
            refreshed_team_two = Team.query.filter_by(id=team_two_id).first()
            assert refreshed_team_one is not None
            assert refreshed_team_two is not None
            assert refreshed_team_one.presentation_completed is False
            assert refreshed_team_two.presentation_completed is False
            assert refreshed_team_one.presentation_completed_at is None
            assert refreshed_team_two.presentation_completed_at is None
            assert refreshed_team_one.presentation_elapsed_seconds is None
            assert refreshed_team_two.presentation_elapsed_seconds is None
    finally:
        with app.app_context():
            Team.query.filter(Team.id.in_([team_one_id, team_two_id])).delete(synchronize_session=False)
            db.session.commit()


def test_admin_updates_presentation_time_limit_option(app, admin_client):
    response = admin_client.post(
        "/admin/options/presentation-time-limit",
        data={"presentation_time_limit_minutes": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    with app.app_context():
        setting = SystemSetting.query.filter_by(key="presentation_time_limit_seconds").first()
        assert setting is not None
        assert setting.value == "420"

        # Keep tests isolated by restoring default value.
        setting.value = "300"
        db.session.commit()


def test_admin_dashboard_active_judges_stat_is_non_negative(admin_client):
    response = admin_client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    match = re.search(r"Active Judges</p>\s*<h2[^>]*>(-?\d+)</h2>", html)
    assert match is not None
    assert int(match.group(1)) >= 0


def test_admin_load_data_page_and_template_download(admin_client):
    page_response = admin_client.get("/admin/load-data", follow_redirects=False)
    assert page_response.status_code == 200
    page_text = page_response.get_data(as_text=True)
    assert "Load Data" in page_text
    assert "Preview Structure" in page_text

    template_response = admin_client.get("/admin/load-data/template", follow_redirects=False)
    assert template_response.status_code == 200
    assert template_response.headers.get("Content-Type", "").startswith("application/json")

    payload = json.loads(template_response.get_data(as_text=True))
    assert isinstance(payload, dict)
    assert "teams" in payload
    assert "judges" in payload


def test_admin_load_data_preview_and_append_import(app, admin_client):
    process_name = _unique_name("proc_load")
    theme_name = _unique_name("theme_load")
    team_name = _unique_name("team_load")
    judge_username = _unique_name("judge_load")
    judge_display_name = f"Judge {uuid.uuid4().hex[:6]}"
    member_email = f"member_{uuid.uuid4().hex[:8]}@example.com"
    team_login_id = _unique_name("team_login")

    load_payload = {
        "processes": [process_name],
        "themes": [theme_name],
        "teams": [
            {
                "team_name": team_name,
                "process": process_name,
                "theme": theme_name,
                "project": {
                    "project_title": "Load Data Project",
                    "problem_statement": "Load data problem",
                    "project_summary": "Load data summary",
                },
                "portal_access": {
                    "login_id": team_login_id,
                    "password": "PortalPass123",
                },
                "members": [
                    {
                        "full_name": "Load Member",
                        "email": member_email,
                    }
                ],
            }
        ],
        "judges": [
            {
                "display_name": judge_display_name,
                "username": judge_username,
                "password": "JudgePass123",
                "organization": "Load Org",
                "is_active": True,
            }
        ],
    }

    try:
        preview_response = admin_client.post(
            "/admin/load-data/preview",
            data={
                "import_mode": "append",
                "json_payload": json.dumps(load_payload),
            },
            follow_redirects=False,
        )
        assert preview_response.status_code == 200
        preview_text = preview_response.get_data(as_text=True)
        assert "Structure Preview" in preview_text
        assert team_name in preview_text
        assert judge_username in preview_text

        import_response = admin_client.post(
            "/admin/load-data/import",
            data={
                "import_mode": "append",
                "json_payload": json.dumps(load_payload),
            },
            follow_redirects=False,
        )
        assert import_response.status_code == 200
        import_text = import_response.get_data(as_text=True)
        assert "Import Completed" in import_text

        with app.app_context():
            process = ProcessOption.query.filter_by(name=process_name).first()
            theme = ThemeOption.query.filter_by(name=theme_name).first()
            team = Team.query.filter_by(team_name=team_name).first()
            user = User.query.filter_by(username=judge_username).first()

            assert process is not None
            assert theme is not None
            assert team is not None
            assert team.portal_login_id == team_login_id
            assert team.project is not None
            assert team.project.project_title == "Load Data Project"
            assert TeamMember.query.filter_by(team_id=team.id, email=member_email).first() is not None

            assert user is not None
            assert user.role == "judge"
            assert user.judge_profile is not None
            assert user.judge_profile.display_name == judge_display_name
    finally:
        with app.app_context():
            user = User.query.filter_by(username=judge_username).first()
            team = Team.query.filter_by(team_name=team_name).first()
            process = ProcessOption.query.filter_by(name=process_name).first()
            theme = ThemeOption.query.filter_by(name=theme_name).first()

            if team:
                db.session.delete(team)
            if user:
                db.session.delete(user)
            if process:
                db.session.delete(process)
            if theme:
                db.session.delete(theme)
            db.session.commit()


def test_admin_load_data_clear_mode_requires_password(app, admin_client):
    seed_team_name = _unique_name("seed_team_load")
    process_name = _unique_name("proc_clear")
    theme_name = _unique_name("theme_clear")

    with app.app_context():
        seeded_team = Team(
            team_name=seed_team_name,
            process=process_name,
            theme=theme_name,
            is_active=True,
        )
        seeded_team.project = Project(
            project_title="Seed Project",
            problem_statement="Seed Problem",
            project_summary="Seed Summary",
        )
        db.session.add(seeded_team)
        db.session.commit()
        seeded_team_id = seeded_team.id

    payload = {
        "teams": [
            {
                "team_name": _unique_name("incoming_team"),
                "process": "General",
                "theme": "General",
                "project": {
                    "project_title": "Incoming Project",
                    "problem_statement": "Incoming Problem",
                    "project_summary": "Incoming Summary",
                },
                "members": [],
            }
        ],
        "judges": [
            {
                "display_name": "Incoming Judge",
            }
        ],
    }

    try:
        missing_password_response = admin_client.post(
            "/admin/load-data/import",
            data={
                "import_mode": "clear_load",
                "json_payload": json.dumps(payload),
                "admin_password": "",
            },
            follow_redirects=False,
        )
        assert missing_password_response.status_code == 200
        missing_password_text = missing_password_response.get_data(as_text=True)
        assert "Admin password is required for Clear All Data and Load New Data mode." in missing_password_text

        invalid_password_response = admin_client.post(
            "/admin/load-data/import",
            data={
                "import_mode": "clear_load",
                "json_payload": json.dumps(payload),
                "admin_password": "invalid-password",
            },
            follow_redirects=False,
        )
        assert invalid_password_response.status_code == 200
        invalid_password_text = invalid_password_response.get_data(as_text=True)
        assert "Invalid admin password. Data load cancelled." in invalid_password_text

        with app.app_context():
            preserved_team = Team.query.filter_by(id=seeded_team_id).first()
            assert preserved_team is not None
    finally:
        with app.app_context():
            seeded_team = Team.query.filter_by(id=seeded_team_id).first()
            if seeded_team:
                db.session.delete(seeded_team)
            db.session.commit()


def test_judge_score_edit_flow_without_lock(app, client):
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

        final_save_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "innovation_originality": "5",
                "technical_implementation": "5",
                "business_value_impact": "5",
                "presentation_clarity": "5",
                "remarks": "Final Save",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert final_save_response.status_code == 302

        with app.app_context():
            rows = Score.query.filter_by(judge_id=judge_id, team_id=team_id).all()
            assert len(rows) == 4
            assert all(not row.is_locked for row in rows)

            total = round(sum(float(row.weighted_score or 0) for row in rows), 2)
            assert total == 50.0

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


def test_judge_can_clear_scores_for_current_team(app, client):
    judge_username = _unique_name("judge_clear")
    judge_password = "judgepass123"
    team_name = _unique_name("team_clear")

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash(judge_password),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Clear", is_active=True)

        team = Team(team_name=team_name, theme="ClearTest", is_active=True)
        team.project = Project(
            project_title="Clear Project",
            problem_statement="Clear Problem",
            project_summary="Clear Summary",
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

        clear_response = client.post(
            f"/judge/teams/{team_id}/score",
            data={
                "action": "clear",
            },
            follow_redirects=False,
        )
        assert clear_response.status_code == 302

        with app.app_context():
            rows = Score.query.filter_by(judge_id=judge_id, team_id=team_id).all()
            assert len(rows) == 0

            audit = (
                AuditLog.query
                .filter_by(actor_user_id=judge_user_id, action="score_cleared", entity_id=team_id)
                .first()
            )
            assert audit is not None
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
