from decimal import Decimal, InvalidOperation

from flask import current_app, has_app_context
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.score import Score, SCORE_CATEGORIES
from models.scoring import ScoringCategorySetting


CATEGORY_LABELS = {
    "innovation_originality": "Innovation and Originality",
    "technical_implementation": "Technical Implementation",
    "business_value_impact": "Business Value and Impact",
    "presentation_clarity": "Presentation and Clarity",
}

DEFAULT_SCORING_RULES = {
    "innovation_originality": {"weight_percent": Decimal("30.00"), "max_score": Decimal("10.00")},
    "technical_implementation": {"weight_percent": Decimal("30.00"), "max_score": Decimal("10.00")},
    "business_value_impact": {"weight_percent": Decimal("25.00"), "max_score": Decimal("10.00")},
    "presentation_clarity": {"weight_percent": Decimal("15.00"), "max_score": Decimal("10.00")},
}


def _to_decimal(value, fallback):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(fallback)
    return parsed


def get_scoring_rules_map():
    rules = {key: value.copy() for key, value in DEFAULT_SCORING_RULES.items()}

    if not has_app_context():
        return rules

    try:
        rows = ScoringCategorySetting.query.all()
    except SQLAlchemyError:
        return rules

    for row in rows:
        if row.category not in rules:
            continue
        rules[row.category] = {
            "weight_percent": _to_decimal(row.weight_percent, rules[row.category]["weight_percent"]),
            "max_score": _to_decimal(row.max_score, rules[row.category]["max_score"]),
        }

    return rules


def get_category_definitions():
    rules = get_scoring_rules_map()
    definitions = []
    for category_key in SCORE_CATEGORIES:
        weight = rules[category_key]["weight_percent"]
        max_score = rules[category_key]["max_score"]
        multiplier = Decimal("0") if max_score <= 0 else (weight / max_score)
        definitions.append(
            {
                "key": category_key,
                "label": CATEGORY_LABELS[category_key],
                "weight_percent": float(weight),
                "max_score": float(max_score),
                "multiplier": float(multiplier),
            }
        )
    return definitions


def clamp_raw_score(category_key, raw_score):
    rules = get_scoring_rules_map()
    max_score = rules[category_key]["max_score"]
    score_decimal = _to_decimal(raw_score, "0")

    if score_decimal < 0:
        score_decimal = Decimal("0")
    if score_decimal > max_score:
        score_decimal = max_score

    return float(score_decimal)


def calculate_weighted_score(category_key, raw_score, rules_map=None):
    rules = rules_map or get_scoring_rules_map()
    weight = _to_decimal(rules[category_key]["weight_percent"], "0")
    max_score = _to_decimal(rules[category_key]["max_score"], "10")
    score_decimal = _to_decimal(raw_score, "0")

    if max_score <= 0:
        return 0.0

    if score_decimal < 0:
        score_decimal = Decimal("0")
    if score_decimal > max_score:
        score_decimal = max_score

    weighted = (score_decimal / max_score) * weight
    return float(weighted.quantize(Decimal("0.01")))


def recalculate_all_weighted_scores(rules_map=None):
    rules = rules_map or get_scoring_rules_map()

    rows = Score.query.all()
    for row in rows:
        row.weighted_score = calculate_weighted_score(row.category, row.raw_score, rules)

    db.session.flush()


def normalize_scoring_updates(form_data):
    updates = {}
    total_weight = Decimal("0")

    for category_key in SCORE_CATEGORIES:
        weight_key = f"weight_{category_key}"
        max_key = f"max_{category_key}"

        raw_weight = form_data.get(weight_key, "").strip()
        raw_max = form_data.get(max_key, "").strip()

        if not raw_weight or not raw_max:
            raise ValueError("All weight and max score values are required.")

        try:
            weight = Decimal(raw_weight)
            max_score = Decimal(raw_max)
        except InvalidOperation as exc:
            raise ValueError("Weights and max scores must be valid numbers.") from exc

        if weight <= 0:
            raise ValueError("Weight percentages must be greater than 0.")
        if max_score <= 0:
            raise ValueError("Maximum scores must be greater than 0.")

        total_weight += weight
        updates[category_key] = {
            "weight_percent": weight.quantize(Decimal("0.01")),
            "max_score": max_score.quantize(Decimal("0.01")),
        }

    if total_weight.quantize(Decimal("0.01")) != Decimal("100.00"):
        raise ValueError("Total of all category percentages must be exactly 100.")

    return updates


def save_scoring_updates(updates):
    for category_key in SCORE_CATEGORIES:
        row = ScoringCategorySetting.query.filter_by(category=category_key).first()
        if not row:
            row = ScoringCategorySetting(category=category_key)
            db.session.add(row)

        row.weight_percent = updates[category_key]["weight_percent"]
        row.max_score = updates[category_key]["max_score"]

    recalculate_all_weighted_scores(rules_map=updates)
    db.session.commit()


def ensure_default_scoring_settings():
    existing_categories = {row.category for row in ScoringCategorySetting.query.all()}
    changed = False
    for category_key in SCORE_CATEGORIES:
        if category_key in existing_categories:
            continue

        defaults = DEFAULT_SCORING_RULES[category_key]
        db.session.add(
            ScoringCategorySetting(
                category=category_key,
                weight_percent=defaults["weight_percent"],
                max_score=defaults["max_score"],
            )
        )
        changed = True

    if changed:
        db.session.flush()
        recalculate_all_weighted_scores()
        db.session.commit()
        current_app.logger.info("Default scoring settings initialized.")
