from . import db


class JudgePresence(db.Model):
    __tablename__ = "judge_presence"

    id = db.Column(db.BigInteger, primary_key=True)
    judge_id = db.Column(
        db.BigInteger,
        db.ForeignKey("judges.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    is_online = db.Column(db.Boolean, nullable=False, default=False)
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    judge = db.relationship("Judge", back_populates="presence", lazy="joined")
