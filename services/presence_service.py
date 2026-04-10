from datetime import datetime, timedelta, timezone

from models import db
from models.presence import JudgePresence


ONLINE_TTL_SECONDS = 20


def _utcnow():
    return datetime.now(timezone.utc)


def mark_judge_online(judge_id):
    presence = JudgePresence.query.filter_by(judge_id=judge_id).first()
    if not presence:
        presence = JudgePresence(judge_id=judge_id)
        db.session.add(presence)

    presence.is_online = True
    presence.last_seen_at = _utcnow()
    db.session.commit()


def mark_judge_offline(judge_id):
    presence = JudgePresence.query.filter_by(judge_id=judge_id).first()
    if not presence:
        presence = JudgePresence(judge_id=judge_id)
        db.session.add(presence)

    presence.is_online = False
    presence.last_seen_at = _utcnow()
    db.session.commit()


def get_judge_online_map(judge_ids):
    if not judge_ids:
        return {}

    threshold = _utcnow() - timedelta(seconds=ONLINE_TTL_SECONDS)
    rows = JudgePresence.query.filter(JudgePresence.judge_id.in_(judge_ids)).all()

    online_map = {judge_id: False for judge_id in judge_ids}
    for row in rows:
        online_map[row.judge_id] = bool(row.is_online and row.last_seen_at and row.last_seen_at >= threshold)

    return online_map
