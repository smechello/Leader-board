from datetime import datetime, timezone
from threading import Lock
from time import monotonic

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

SCOREBOARD_CACHE_TTL_SECONDS = 5.0
_scoreboard_cache_lock = Lock()
_scoreboard_cache = {
    "created_at_monotonic": 0.0,
    "generated_at": None,
    "rows": None,
}


def get_scoreboard_tie_break_rule():
    return SCOREBOARD_TIE_BREAK_RULE


def _clone_rows(rows):
    return [dict(row) for row in (rows or [])]


def _query_live_scoreboard_rows():
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


def clear_scoreboard_cache():
    with _scoreboard_cache_lock:
        _scoreboard_cache["created_at_monotonic"] = 0.0
        _scoreboard_cache["generated_at"] = None
        _scoreboard_cache["rows"] = None


def get_cached_live_scoreboard_snapshot(max_age_seconds=SCOREBOARD_CACHE_TTL_SECONDS, force_refresh=False):
    # Keep tests deterministic and avoid stale assertions.
    if force_refresh or current_app.config.get("TESTING"):
        return {
            "rows": _query_live_scoreboard_rows(),
            "generated_at": datetime.now(timezone.utc),
            "cache_hit": False,
        }

    now_monotonic = monotonic()
    with _scoreboard_cache_lock:
        cached_rows = _scoreboard_cache.get("rows")
        cached_at = float(_scoreboard_cache.get("created_at_monotonic") or 0.0)
        if cached_rows is not None and (now_monotonic - cached_at) < float(max_age_seconds):
            return {
                "rows": _clone_rows(cached_rows),
                "generated_at": _scoreboard_cache.get("generated_at"),
                "cache_hit": True,
            }

    fresh_rows = _query_live_scoreboard_rows()
    generated_at = datetime.now(timezone.utc)

    with _scoreboard_cache_lock:
        _scoreboard_cache["rows"] = _clone_rows(fresh_rows)
        _scoreboard_cache["generated_at"] = generated_at
        _scoreboard_cache["created_at_monotonic"] = now_monotonic

    return {
        "rows": fresh_rows,
        "generated_at": generated_at,
        "cache_hit": False,
    }


def get_live_scoreboard_rows():
    return _query_live_scoreboard_rows()
