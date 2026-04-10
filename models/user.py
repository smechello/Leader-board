from flask_login import UserMixin
from sqlalchemy import Enum

from . import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.BigInteger, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    password_hash = db.Column(db.Text, nullable=False)
    role = db.Column(Enum("admin", "judge", name="user_role"), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    judge_profile = db.relationship(
        "Judge",
        uselist=False,
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def get_id(self):
        if self.role == "judge":
            return f"judge:{self.id}"
        return str(self.id)


class Judge(db.Model):
    __tablename__ = "judges"

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(
        db.BigInteger,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    display_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    organization = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    user = db.relationship("User", back_populates="judge_profile")
    scores = db.relationship("Score", back_populates="judge", cascade="all, delete-orphan")
    presence = db.relationship(
        "JudgePresence",
        uselist=False,
        back_populates="judge",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
