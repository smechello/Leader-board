from sqlalchemy import UniqueConstraint

from . import db


class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.BigInteger, primary_key=True)
    team_name = db.Column(db.String(120), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0, index=True)
    portal_login_id = db.Column(db.String(80), nullable=True, unique=True, index=True)
    portal_password_hash = db.Column(db.Text, nullable=True)
    process = db.Column(db.String(120), nullable=False, default="General")
    theme = db.Column(db.String(120), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    members = db.relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    project = db.relationship(
        "Project",
        uselist=False,
        back_populates="team",
        cascade="all, delete-orphan",
    )
    scores = db.relationship("Score", back_populates="team", cascade="all, delete-orphan")


class TeamMember(db.Model):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "email", name="uq_team_members_team_email"),)

    id = db.Column(db.BigInteger, primary_key=True)
    team_id = db.Column(
        db.BigInteger,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    department_or_class = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    team = db.relationship("Team", back_populates="members")


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.BigInteger, primary_key=True)
    team_id = db.Column(
        db.BigInteger,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    project_title = db.Column(db.String(200), nullable=False)
    problem_statement = db.Column(db.Text, nullable=False)
    project_summary = db.Column(db.Text, nullable=False)
    repository_url = db.Column(db.Text, nullable=True)
    demo_url = db.Column(db.Text, nullable=True)
    notes_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    team = db.relationship("Team", back_populates="project")
