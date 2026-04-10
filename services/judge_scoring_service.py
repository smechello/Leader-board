from collections import defaultdict

from models import db
from models.score import SCORE_CATEGORIES, Score
from models.team import Team

CATEGORY_DEFINITIONS = (
    {
        "key": "innovation_originality",
        "label": "Innovation and Originality",
        "weight_percent": 30,
        "multiplier": 3.0,
    },
    {
        "key": "technical_implementation",
        "label": "Technical Implementation",
        "weight_percent": 30,
        "multiplier": 3.0,
    },
    {
        "key": "business_value_impact",
        "label": "Business Value and Impact",
        "weight_percent": 25,
        "multiplier": 2.5,
    },
    {
        "key": "presentation_clarity",
        "label": "Presentation and Clarity",
        "weight_percent": 15,
        "multiplier": 1.5,
    },
)

CATEGORY_COUNT = len(CATEGORY_DEFINITIONS)
CATEGORY_LABELS = {item["key"]: item["label"] for item in CATEGORY_DEFINITIONS}
CATEGORY_MULTIPLIERS = {item["key"]: item["multiplier"] for item in CATEGORY_DEFINITIONS}


def calculate_total_from_raw_scores(raw_scores):
    total = 0.0
    for category_key in SCORE_CATEGORIES:
        value = float(raw_scores.get(category_key, 0) or 0)
        total += value * CATEGORY_MULTIPLIERS[category_key]
    return round(total, 2)


def get_judge_team_score_snapshot(judge_id, team_id):
    rows = Score.query.filter_by(judge_id=judge_id, team_id=team_id).all()

    score_values = {category_key: None for category_key in SCORE_CATEGORIES}
    remarks = ""
    weighted_total = 0.0
    scored_categories = set()

    for row in rows:
        score_values[row.category] = float(row.raw_score)
        weighted_total += float(row.weighted_score or 0)
        scored_categories.add(row.category)
        if row.remarks and not remarks:
            remarks = row.remarks

    return {
        "score_values": score_values,
        "remarks": remarks,
        "categories_scored": len(scored_categories),
        "weighted_total": round(weighted_total, 2),
        "is_complete": len(scored_categories) == CATEGORY_COUNT,
    }


def get_judge_dashboard_rows(judge_id):
    teams = Team.query.filter(Team.is_active.is_(True)).order_by(Team.id.asc()).all()
    if not teams:
        return []

    team_ids = [team.id for team in teams]
    score_rows = Score.query.filter(
        Score.judge_id == judge_id,
        Score.team_id.in_(team_ids),
    ).all()

    grouped_rows = defaultdict(list)
    for row in score_rows:
        grouped_rows[row.team_id].append(row)

    dashboard_rows = []
    for team in teams:
        team_scores = grouped_rows.get(team.id, [])
        categories_scored = len({row.category for row in team_scores})
        judge_total = round(sum(float(row.weighted_score or 0) for row in team_scores), 2)

        dashboard_rows.append(
            {
                "id": team.id,
                "team_name": team.team_name,
                "theme": team.theme,
                "project_title": team.project.project_title if team.project else "-",
                "project_summary": team.project.project_summary if team.project else "-",
                "categories_scored": categories_scored,
                "judge_total": judge_total,
                "is_completed": categories_scored == CATEGORY_COUNT,
            }
        )

    return dashboard_rows


def save_or_update_judge_scores(judge_id, team_id, raw_scores, remarks):
    existing_rows = Score.query.filter_by(judge_id=judge_id, team_id=team_id).all()
    existing_by_category = {row.category: row for row in existing_rows}

    normalized_remarks = (remarks or "").strip() or None

    for category_key in SCORE_CATEGORIES:
        if category_key not in raw_scores:
            raise ValueError(f"Missing score for category: {CATEGORY_LABELS[category_key]}")

        try:
            score_value = float(raw_scores[category_key])
        except (TypeError, ValueError):
            raise ValueError(f"{CATEGORY_LABELS[category_key]} must be a valid number.")

        if score_value < 0 or score_value > 10:
            raise ValueError(f"{CATEGORY_LABELS[category_key]} must be between 0 and 10.")

        row = existing_by_category.get(category_key)
        if row:
            row.raw_score = score_value
            row.remarks = normalized_remarks
        else:
            db.session.add(
                Score(
                    judge_id=judge_id,
                    team_id=team_id,
                    category=category_key,
                    raw_score=score_value,
                    remarks=normalized_remarks,
                )
            )

    db.session.commit()


def get_next_active_team_id(current_team_id):
    next_team = (
        Team.query.filter(Team.is_active.is_(True), Team.id > current_team_id)
        .order_by(Team.id.asc())
        .first()
    )
    if not next_team:
        return None
    return next_team.id
