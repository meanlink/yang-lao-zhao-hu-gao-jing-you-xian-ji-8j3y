"""Shared helpers for revocation workflow tests."""
from src.auth import hash_password
from src.db import connect
from src.priority_model import PriorityModel
from src.schemas import Handover, NewObservation, Review, Signals
from src.workflow import dispatch, enter_observation, lock_alert, review, score_one

MODEL = PriorityModel()


def seed_staff():
    with connect() as db:
        rows = {}
        for email, name, role in [
            ('carer@test.local', '照护员甲', 'carer'),
            ('lead@test.local', '值班长乙', 'lead'),
            ('receiver@test.local', '接班员丙', 'receiver'),
        ]:
            row = db.execute(
                'INSERT INTO staff(email,name,password_hash,role) VALUES (%s,%s,%s,%s) RETURNING *',
                (email, name, hash_password('pw'), role),
            ).fetchone()
            rows[role] = row['id']
        return rows


def seed_resident(code='ZH-901', room='901-A', unit='测试翼一层'):
    with connect() as db:
        return db.execute(
            'INSERT INTO residents(code,name,room,unit,care_plan) VALUES (%s,%s,%s,%s,%s) RETURNING *',
            (code, '测试住户', room, unit, '测试照护要点。'),
        ).fetchone()


def observe(staff, resident_id, ref, signals, note='巡房观察测试记录。'):
    with connect() as db:
        return enter_observation(db, NewObservation(
            ref=ref, resident_id=resident_id, author_note=note, **signals), staff)


def score_resident(resident_id):
    with connect() as db:
        scored = []
        while job := score_one(db, MODEL, resident_id):
            scored.append(job)
        return scored


def full_signals(a, b, c, d):
    return Signals(missed_round=a, repeated_call=b, transfer_support=c, unfinished_task=d).model_dump()


def review_and_dispatch(lead_id, alert_id, priority=None, action='核对测试住户的接续事项。'):
    with connect() as db:
        alert = lock_alert(db, alert_id)
        assert alert['status'] == 'review', alert
        chosen = priority or alert['priority']
        review(db, alert_id, Review(revision=alert['revision'], priority=chosen,
                                    note='值班长复核说明，撤销重算测试。'), lead_id)
        alert = lock_alert(db, alert_id)
        handover = dispatch(db, alert_id, Handover(
            revision=alert['revision'], target_shift='night', next_action=action), lead_id)
        return handover
