from services.judge_scoring_service import calculate_total_from_raw_scores


def test_calculate_total_from_raw_scores():
    raw_scores = {
        "innovation_originality": 9,
        "technical_implementation": 7,
        "business_value_impact": 9,
        "presentation_clarity": 6,
    }

    total = calculate_total_from_raw_scores(raw_scores)

    assert total == 79.5
