import uuid

from werkzeug.security import check_password_hash, generate_password_hash

from models import db
from models.auth_access import JudgeDirectLoginLink
from models.user import Judge, User


def _unique_name(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_admin_can_update_judge_password_and_revoke_declined_direct_link(app, admin_client):
    judge_username = _unique_name("judge_direct_link")
    judge_client = app.test_client()

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash("old_password_123"),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name="Judge Direct Link", is_active=True)
        db.session.add(judge_user)
        db.session.commit()
        judge_user_id = judge_user.id

    try:
        password_response = admin_client.post(
            f"/admin/judges/{judge_user_id}/password",
            data={"new_password": "new_password_456"},
            follow_redirects=False,
        )
        assert password_response.status_code == 302

        with app.app_context():
            updated_user = User.query.filter_by(id=judge_user_id).first()
            assert updated_user is not None
            assert check_password_hash(updated_user.password_hash, "new_password_456")

        link_response = admin_client.post(
            f"/admin/judges/{judge_user_id}/direct-link",
            data={"lifespan_minutes": "30"},
            follow_redirects=False,
        )
        assert link_response.status_code == 302

        with app.app_context():
            judge = Judge.query.filter_by(user_id=judge_user_id).first()
            assert judge is not None
            direct_link = JudgeDirectLoginLink.query.filter_by(judge_id=judge.id).order_by(JudgeDirectLoginLink.id.desc()).first()
            assert direct_link is not None
            token = direct_link.token
            link_id = direct_link.id

        confirm_page = judge_client.get(f"/judge/direct-login/{token}")
        assert confirm_page.status_code == 200

        decline_response = judge_client.post(
            f"/judge/direct-login/{token}",
            data={"decision": "no"},
            follow_redirects=False,
        )
        assert decline_response.status_code == 302
        assert "/login" in (decline_response.headers.get("Location") or "")

        with app.app_context():
            revoked_link = JudgeDirectLoginLink.query.filter_by(id=link_id).first()
            assert revoked_link is not None
            assert revoked_link.revoked_at is not None
    finally:
        with app.app_context():
            user = User.query.filter_by(id=judge_user_id).first()
            if user:
                db.session.delete(user)
            db.session.commit()


def test_judge_login_request_approval_allows_direct_login(app, admin_client):
    judge_username = _unique_name("judge_request_login")
    judge_display_name = "Judge Request Access"
    judge_client = app.test_client()

    with app.app_context():
        judge_user = User(
            username=judge_username,
            email=f"{judge_username}@example.com",
            password_hash=generate_password_hash("judge_password_123"),
            role="judge",
            is_active=True,
        )
        Judge(user=judge_user, display_name=judge_display_name, is_active=True)
        db.session.add(judge_user)
        db.session.commit()
        judge_user_id = judge_user.id

    try:
        request_response = judge_client.post(
            "/login/request-access",
            json={"username": judge_username},
        )
        assert request_response.status_code == 200
        request_payload = request_response.get_json()
        request_id = request_payload["request_id"]
        request_key = request_payload["request_key"]

        notifications_response = admin_client.get("/admin/notifications/login-requests")
        assert notifications_response.status_code == 200
        assert notifications_response.get_json()["count"] >= 1

        approve_response = admin_client.post(
            f"/admin/login-requests/{request_id}/approve",
            follow_redirects=False,
        )
        assert approve_response.status_code == 302

        status_response = judge_client.get(f"/login/request-status/{request_id}?key={request_key}")
        assert status_response.status_code == 200
        assert status_response.get_json()["status"] == "approved"

        consume_response = judge_client.post(
            "/login/request-consume",
            json={"request_id": request_id, "request_key": request_key},
        )
        assert consume_response.status_code == 200
        consume_payload = consume_response.get_json()
        assert consume_payload["status"] == "consumed"

        dashboard_response = judge_client.get("/judge/dashboard", follow_redirects=False)
        assert dashboard_response.status_code == 200
    finally:
        with app.app_context():
            user = User.query.filter_by(id=judge_user_id).first()
            if user:
                db.session.delete(user)
            db.session.commit()
