from sqlalchemy import CheckConstraint, Enum, UniqueConstraint, event

from . import db


SCORE_CATEGORIES = (
    "innovation_originality",
    "technical_implementation",
    "business_value_impact",
    "presentation_clarity",
)


class Score(db.Model):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint(
            "judge_id",
            "team_id",
            "category",
            name="uq_scores_judge_team_category",
        ),
        CheckConstraint("raw_score >= 0", name="ck_scores_raw_score_non_negative"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    judge_id = db.Column(
        db.BigInteger,
        db.ForeignKey("judges.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id = db.Column(
        db.BigInteger,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    )
    category = db.Column(Enum(*SCORE_CATEGORIES, name="score_category"), nullable=False)
    raw_score = db.Column(db.Numeric(4, 2), nullable=False)
    weighted_score = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    remarks = db.Column(db.Text, nullable=True)
    is_locked = db.Column(db.Boolean, nullable=False, default=False)
    submitted_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    judge = db.relationship("Judge", back_populates="scores")
    team = db.relationship("Team", back_populates="scores")


def _sync_weighted_score(mapper, connection, target):
    """Keep weighted_score aligned with raw_score and current category rules."""
    from services.scoring_config_service import calculate_weighted_score

    try:
        raw_score = float(target.raw_score or 0)
    except (TypeError, ValueError):
        raw_score = 0.0

    target.weighted_score = calculate_weighted_score(target.category, raw_score)


event.listen(Score, "before_insert", _sync_weighted_score)
event.listen(Score, "before_update", _sync_weighted_score)
