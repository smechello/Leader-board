from flask import current_app
from sqlalchemy import case, func
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.score import Score
from models.team import Team


SCOREBOARD_TIE_BREAK_RULE = (
    "Tie-break rule: higher Business Value and Impact score first, "
    "then earliest score submission time."
)


def get_scoreboard_tie_break_rule():
    return SCOREBOARD_TIE_BREAK_RULE


def get_live_scoreboard_rows():
    total_score = func.coalesce(func.sum(Score.weighted_score), 0).label("total_score")
    business_value_score = func.coalesce(
        func.sum(
            case(
                (Score.category == "business_value_impact", Score.weighted_score),
                else_=0,
            )
        ),
        0,
    ).label("business_value_score")
    earliest_submission = func.min(Score.submitted_at).label("earliest_submission")

    try:
        rows = (
            db.session.query(
                Team.id,
                Team.team_name,
                Team.process,
                Team.theme,
                total_score,
                business_value_score,
                earliest_submission,
            )
            .outerjoin(Score, Score.team_id == Team.id)
            .filter(Team.is_active.is_(True))
            .group_by(Team.id, Team.team_name, Team.process, Team.theme)
            .order_by(
                total_score.desc(),
                business_value_score.desc(),
                earliest_submission.asc().nullslast(),
                Team.team_name.asc(),
            )
            .all()
        )
    except SQLAlchemyError as exc:
        current_app.logger.warning("Scoreboard query unavailable: %s", exc)
        return []

    return [
        {
            "rank": index,
            "team_name": row.team_name,
            "process": row.process,
            "theme": row.theme,
            "total_score": float(row.total_score or 0.0),
            "business_value_score": float(row.business_value_score or 0.0),
            "earliest_submission": (
                row.earliest_submission.isoformat() if row.earliest_submission else None
            ),
        }
        for index, row in enumerate(rows, start=1)
    ]
