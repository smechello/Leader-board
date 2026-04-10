from . import db


class ScoringCategorySetting(db.Model):
    __tablename__ = "scoring_category_settings"

    id = db.Column(db.BigInteger, primary_key=True)
    category = db.Column(db.String(64), nullable=False, unique=True, index=True)
    weight_percent = db.Column(db.Numeric(6, 2), nullable=False)
    max_score = db.Column(db.Numeric(6, 2), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )
