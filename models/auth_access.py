from . import db


LOGIN_REQUEST_STATUS_PENDING = "pending"
LOGIN_REQUEST_STATUS_APPROVED = "approved"
LOGIN_REQUEST_STATUS_REJECTED = "rejected"
LOGIN_REQUEST_STATUS_EXPIRED = "expired"
LOGIN_REQUEST_STATUS_CONSUMED = "consumed"


class JudgeDirectLoginLink(db.Model):
    __tablename__ = "judge_direct_login_links"

    id = db.Column(db.BigInteger, primary_key=True)
    judge_id = db.Column(
        db.BigInteger,
        db.ForeignKey("judges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = db.Column(db.String(128), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoke_reason = db.Column(db.String(120), nullable=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by_admin = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    judge = db.relationship(
        "Judge",
        backref=db.backref(
            "direct_login_links",
            lazy="dynamic",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
    )


class TeamDirectLoginLink(db.Model):
    __tablename__ = "team_direct_login_links"

    id = db.Column(db.BigInteger, primary_key=True)
    team_id = db.Column(
        db.BigInteger,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = db.Column(db.String(128), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoke_reason = db.Column(db.String(120), nullable=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by_admin = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    team = db.relationship(
        "Team",
        backref=db.backref(
            "direct_login_links",
            lazy="dynamic",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
    )


class JudgeLoginRequest(db.Model):
    __tablename__ = "judge_login_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    judge_id = db.Column(
        db.BigInteger,
        db.ForeignKey("judges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_key = db.Column(db.String(128), nullable=False, unique=True, index=True)
    requested_login = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=LOGIN_REQUEST_STATUS_PENDING, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    decided_by_admin = db.Column(db.String(80), nullable=True)
    approval_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    consumed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    judge = db.relationship(
        "Judge",
        backref=db.backref(
            "login_requests",
            lazy="dynamic",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
    )
