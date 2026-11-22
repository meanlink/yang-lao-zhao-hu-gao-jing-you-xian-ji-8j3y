"""撤销记错巡房观察的端到端工作流测试（直接驱动工作流函数，覆盖真实 SQL 约束）。"""
import threading

import psycopg
import pytest
from fastapi import HTTPException

from src.db import connect
from src.schemas import NewObservation, Receipt, Review, Signals
from src.workflow import (dispatch, enter_observation, lock_alert, recompute_status,
                          revoke_observation, review, sign, supplement)

from .helpers import MODEL, full_signals, observe, review_and_dispatch, score_resident, seed_resident, seed_staff


@pytest.fixture()
def ids(clean_db):
    staff = seed_staff()
    resident = seed_resident()
    return staff, resident


def fetch(db, sql, *params):
    return db.execute(sql, params).fetchone()


def revoke(observation_id, reason, lead_id):
    with connect() as db:
        return revoke_observation(db, observation_id, reason, lead_id)


def test_queued_observation_revoked_is_archived_and_excluded_from_scoring(ids):
    staff, resident = ids
    first = observe(staff['carer'], resident['id'], 'OBS-T-0001',
                    full_signals(True, True, True, True))
    queued = observe(staff['carer'], resident['id'], 'OBS-T-0002',
                     full_signals(False, False, False, False))
    observation, revocation, alert = revoke(
        queued['id'], '走廊巡查单记错房号，登记到本住户名下。', staff['lead'])
    assert observation['status'] == 'revoked'
    assert revocation['previous_status'] == 'queued'
    assert revocation['remaining_observations'] == 1
    # 撤销时仍在排队的观察不再计分：worker 只能领取剩余的另一条。
    scored = score_resident(resident['id'])
    assert scored == [first['id']]
    with connect() as db:
        row = fetch(db, "SELECT status,result,scored_at FROM observations WHERE id=%s", queued['id'])
        assert row['status'] == 'revoked' and row['result'] is None and row['scored_at'] is None
        case = fetch(db, "SELECT status,priority,revision FROM alerts WHERE id=%s", alert['id'])
        assert case['status'] == 'review'  # 剩余观察评分后回到待复核
        assert case['revision'] == alert['revision']


def test_revoke_scored_observation_recomputes_priority_from_remaining(ids):
    staff, resident = ids
    high = observe(staff['carer'], resident['id'], 'OBS-T-0101',
                   full_signals(True, True, True, True))    # 候选等级 3
    observe(staff['carer'], resident['id'], 'OBS-T-0102',
            full_signals(False, False, False, False))       # 候选等级 1
    score_resident(resident['id'])
    with connect() as db:
        before = fetch(db, "SELECT priority,status FROM alerts WHERE id=%s", high['alert_id'])
        assert before['priority'] == 3 and before['status'] == 'review'
    observation, revocation, alert = revoke(
        high['id'], '信号来源核实为误记，撤销本条。', staff['lead'])
    with connect() as db:
        case = fetch(db, "SELECT priority,status,revision FROM alerts WHERE id=%s", alert['id'])
        assert case['priority'] == 1  # 按剩余观察重算
        assert case['status'] == 'review'
        remaining = fetch(db,
                          "SELECT count(*) AS n FROM observations WHERE alert_id=%s AND status <> 'revoked'",
                          alert['id'])
        assert remaining['n'] == 1
        # 原观察留档，评分结果仍可追溯。
        archived = fetch(db, "SELECT status,result FROM observations WHERE id=%s", high['id'])
        assert archived['status'] == 'revoked' and archived['result'] is not None
        record = fetch(db,
                       "SELECT previous_status,previous_priority FROM observation_revocations WHERE observation_id=%s",
                       high['id'])
        assert record['previous_status'] == 'scored' and record['previous_priority'] == 3


def test_revoke_last_observation_closes_alert_and_allows_new_round(ids):
    staff, resident = ids
    only = observe(staff['carer'], resident['id'], 'OBS-T-0201',
                   full_signals(True, False, True, False))
    score_resident(resident['id'])
    observation, revocation, alert = revoke(
        only['id'], '整轮巡房单重复登记，没有其他在办观察。', staff['lead'])
    with connect() as db:
        case = fetch(db, "SELECT status,priority FROM alerts WHERE id=%s", alert['id'])
        assert case['status'] == 'closed'
        assert revocation['remaining_observations'] == 0
        # 关闭后该住户可以开启新一轮告警（部分唯一索引不拦截 closed）。
        new_round = enter_observation(db, NewObservation(
            ref='OBS-T-0202', resident_id=resident['id'], author_note='新一轮巡房观察。',
            **full_signals(False, True, False, True)), staff['carer'])
        assert new_round['alert_id'] != alert['id']


def test_revoke_voids_open_handover_but_signed_handover_keeps_original(ids):
    staff, resident = ids
    first = observe(staff['carer'], resident['id'], 'OBS-T-0301',
                    full_signals(True, True, False, False))
    second = observe(staff['carer'], resident['id'], 'OBS-T-0302',
                     full_signals(False, False, False, True))
    score_resident(resident['id'])
    handover = review_and_dispatch(staff['lead'], first['alert_id'])
    with connect() as db:
        assert fetch(db, "SELECT status FROM alerts WHERE id=%s", first['alert_id'])['status'] == 'handover'
    observation, revocation, alert = revoke(
        second['id'], '交班后发现记错，作废待签单并回到复核。', staff['lead'])
    with connect() as db:
        assert revocation['superseded_handover_id'] == handover['id']
        assert fetch(db, "SELECT status FROM handovers WHERE id=%s", handover['id'])['status'] == 'superseded'
        case = fetch(db, "SELECT status,revision FROM alerts WHERE id=%s", alert['id'])
        assert case['status'] == 'review' and case['revision'] > handover['alert_revision']
        # 旧待签单不能再签收。
        with pytest.raises(HTTPException) as exc:
            sign(db, handover['id'], Receipt(receipt_note='尝试签收已作废单。'), staff['receiver'])
        assert exc.value.status_code == 409
    # 新一轮复核、交班并签收，已签交班保留原快照；其中不再含已撤销观察。
    score_resident(resident['id'])
    new_handover = review_and_dispatch(staff['lead'], alert['id'], priority=2,
                                       action='按剩余观察重新接续。')
    with connect() as db:
        signed = sign(db, new_handover['id'], Receipt(receipt_note='接班员核对后签收。'),
                      staff['receiver'])
        assert signed['status'] == 'signed'
        case = fetch(db, "SELECT status FROM alerts WHERE id=%s", alert['id'])
        assert case['status'] == 'closed'
        refs = [item['ref'] for item in signed['snapshot']['observations']]
        assert refs == ['OBS-T-0301']
        # 已签交班按原件查阅，撤销被拒。
        with pytest.raises(HTTPException) as exc:
            revoke_observation(db, first['id'], '归档后不允许撤销。', staff['lead'])
        assert exc.value.status_code == 409
        # 已作废的旧单仍在归档中可查，快照保留撤销前的观察编号。
        old = fetch(db, "SELECT status,snapshot FROM handovers WHERE id=%s", handover['id'])
        assert old['status'] == 'superseded'
        assert {item['ref'] for item in old['snapshot']['observations']} == {'OBS-T-0301', 'OBS-T-0302'}


def test_revoke_incomplete_observation_returns_alert_to_review_when_rest_scored(ids):
    staff, resident = ids
    complete = observe(staff['carer'], resident['id'], 'OBS-T-0401',
                       full_signals(False, False, False, False))
    pending = observe(staff['carer'], resident['id'], 'OBS-T-0402',
                      {'missed_round': None, 'repeated_call': True,
                       'transfer_support': None, 'unfinished_task': False})
    score_resident(resident['id'])
    with connect() as db:
        assert fetch(db, "SELECT status FROM alerts WHERE id=%s",
                     complete['alert_id'])['status'] == 'incomplete'
    observation, revocation, alert = revoke(
        pending['id'], '待补观察确认是错单，直接撤销。', staff['lead'])
    with connect() as db:
        case = fetch(db, "SELECT status FROM alerts WHERE id=%s", alert['id'])
        assert case['status'] == 'review'
        assert fetch(db,
                     "SELECT count(*) AS n FROM observations WHERE status='incomplete' AND alert_id=%s",
                     alert['id'])['n'] == 0


def test_revocation_retry_is_idempotent_and_leaves_single_audit_trail(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-T-0501',
                  full_signals(True, False, False, False))
    reason = '客户端超时重试，同一条撤销请求。'
    _, first_rev, _ = revoke(row['id'], reason, staff['lead'])
    _, second_rev, _ = revoke(row['id'], reason, staff['lead'])  # 重试
    assert first_rev['id'] == second_rev['id']
    with connect() as db:
        assert fetch(db, "SELECT count(*) AS n FROM observation_revocations WHERE observation_id=%s",
                     row['id'])['n'] == 1
        events = db.execute(
            "SELECT event FROM audit_events WHERE subject='observation' AND subject_id=%s ORDER BY id",
            (row['id'],)).fetchall()
        assert [e['event'] for e in events].count('observation_revoked') == 1


def test_supplement_then_revoke_flow(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-T-0601',
                  {'missed_round': True, 'repeated_call': None,
                   'transfer_support': None, 'unfinished_task': None})
    with connect() as db:
        supplement(db, row['id'],
                   Signals(repeated_call=False, transfer_support=False, unfinished_task=False),
                   staff['carer'])
        assert fetch(db, "SELECT status FROM observations WHERE id=%s", row['id'])['status'] == 'queued'
    observation, revocation, alert = revoke(
        row['id'], '补录后核对发现首项信号也记错，整单撤销。', staff['lead'])
    assert observation['status'] == 'revoked'
    with connect() as db:
        assert fetch(db, "SELECT status FROM alerts WHERE id=%s", alert['id'])['status'] == 'closed'


def test_revocation_and_sign_concurrency_first_committed_wins(ids):
    staff, resident = ids
    first = observe(staff['carer'], resident['id'], 'OBS-T-0701',
                    full_signals(True, True, True, False))
    second = observe(staff['carer'], resident['id'], 'OBS-T-0702',
                     full_signals(False, False, False, True))
    score_resident(resident['id'])
    handover = review_and_dispatch(staff['lead'], first['alert_id'])
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_sign():
        barrier.wait()
        try:
            with connect() as db:
                sign(db, handover['id'], Receipt(receipt_note='并发签收。'), staff['receiver'])
            outcomes['sign'] = 'signed'
        except HTTPException as exc:
            outcomes['sign'] = f'blocked:{exc.status_code}'
        except psycopg.Error as exc:
            outcomes['sign'] = f'sql:{type(exc).__name__}'

    def do_revoke():
        barrier.wait()
        try:
            with connect() as db:
                revoke_observation(db, second['id'], '并发撤销记错观察。', staff['lead'])
            outcomes['revoke'] = 'revoked'
        except HTTPException as exc:
            outcomes['revoke'] = f'blocked:{exc.status_code}'
        except psycopg.Error as exc:
            outcomes['revoke'] = f'sql:{type(exc).__name__}'

    sign_thread = threading.Thread(target=do_sign)
    revoke_thread = threading.Thread(target=do_revoke)
    sign_thread.start()
    revoke_thread.start()
    sign_thread.join()
    revoke_thread.join()
    with connect() as db:
        alert_status = fetch(db, "SELECT status FROM alerts WHERE id=%s", first['alert_id'])['status']
        handover_status = fetch(db, "SELECT status FROM handovers WHERE id=%s", handover['id'])['status']
        second_status = fetch(db, "SELECT status FROM observations WHERE id=%s", second['id'])['status']
    # 撤销与签收并发，以先办成的为准；两种结局都必须自洽，且不允许出现 SQL 级错误（死锁/约束冲突）。
    assert not any(str(v).startswith('sql:') for v in outcomes.values()), outcomes
    if outcomes.get('sign') == 'signed':
        assert alert_status == 'closed' and handover_status == 'signed'
        assert second_status == 'scored'
        assert outcomes['revoke'].startswith('blocked')
    else:
        assert second_status == 'revoked' and handover_status == 'superseded'
        assert alert_status == 'review'
        assert outcomes['sign'].startswith('blocked')


def test_recompute_handles_mixed_remaining_observations(ids):
    staff, resident = ids
    observe(staff['carer'], resident['id'], 'OBS-T-0801', full_signals(True, True, True, True))
    keep_incomplete = observe(staff['carer'], resident['id'], 'OBS-T-0802',
                              {'missed_round': None, 'repeated_call': False,
                               'transfer_support': False, 'unfinished_task': False})
    score_resident(resident['id'])
    observe(staff['carer'], resident['id'], 'OBS-T-0803', full_signals(False, False, False, False))
    with connect() as db:
        assert recompute_status(db, keep_incomplete['alert_id']) == 'incomplete'
    # 撤销待补观察后，仍有排队观察 -> evaluating。
    revoke(keep_incomplete['id'], '撤销待补错单。', staff['lead'])
    with connect() as db:
        assert fetch(db, "SELECT status FROM alerts WHERE id=%s",
                     keep_incomplete['alert_id'])['status'] == 'evaluating'
    score_resident(resident['id'])
    with connect() as db:
        assert fetch(db, "SELECT status FROM alerts WHERE id=%s",
                     keep_incomplete['alert_id'])['status'] == 'review'
