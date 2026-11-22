"""撤销端点的 HTTP 层测试：角色权限、请求校验与工作台查询。"""
import pytest
from fastapi.testclient import TestClient

from src.db import connect
from src.main import app

from .helpers import full_signals, observe, score_resident, seed_resident, seed_staff


@pytest.fixture()
def ids(clean_db):
    staff = seed_staff()
    resident = seed_resident()
    return staff, resident


def token(client, email, password='pw'):
    resp = client.post('/auth/login', json={'email': email, 'password': password})
    assert resp.status_code == 200, resp.text
    return resp.json()['access_token']


def auth(tok):
    return {'Authorization': f'Bearer {tok}'}


def test_only_lead_can_revoke_observation(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-A-0001',
                  full_signals(True, False, True, False))
    carer = token(TestClient(app), 'carer@test.local')
    receiver = token(TestClient(app), 'receiver@test.local')
    lead = token(TestClient(app), 'lead@test.local')
    with TestClient(app) as client:
        body = {'reason': '巡房单记错房号，申请撤销该条观察。'}
        denied_carer = client.post(f"/observations/{row['id']}/revoke", json=body, headers=auth(carer))
        denied_receiver = client.post(f"/observations/{row['id']}/revoke", json=body, headers=auth(receiver))
        assert denied_carer.status_code == 403
        assert denied_receiver.status_code == 403
        allowed = client.post(f"/observations/{row['id']}/revoke", json=body, headers=auth(lead))
        assert allowed.status_code == 200, allowed.text
        payload = allowed.json()
        assert payload['observation']['status'] == 'revoked'
        assert payload['revocation']['reason'] == body['reason']
        assert payload['revocation']['previous_status'] == 'queued'
        assert payload['alert']['status'] == 'closed'
        assert payload['alert']['closed_reason'] == 'revoked'


def test_revoke_requires_reason_and_validates_length(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-A-0002',
                  full_signals(False, False, False, True))
    lead = token(TestClient(app), 'lead@test.local')
    with TestClient(app) as client:
        missing = client.post(f"/observations/{row['id']}/revoke", json={}, headers=auth(lead))
        too_short = client.post(f"/observations/{row['id']}/revoke",
                                json={'reason': '错'}, headers=auth(lead))
        assert missing.status_code == 422
        assert too_short.status_code == 422
        # 校验失败不会产生任何撤销留痕。
        with connect() as db:
            count = db.execute('SELECT count(*) AS n FROM observation_revocations').fetchone()['n']
            assert count == 0


def test_revoke_unknown_observation_returns_404(ids):
    lead = token(TestClient(app), 'lead@test.local')
    with TestClient(app) as client:
        resp = client.post('/observations/999999/revoke',
                           json={'reason': '尝试撤销不存在的观察编号。'}, headers=auth(lead))
        assert resp.status_code == 404


def test_workspace_lists_recomputed_alert_and_archived_revocation(ids):
    staff, resident = ids
    high = observe(staff['carer'], resident['id'], 'OBS-A-0101',
                   full_signals(True, True, True, True))
    low = observe(staff['carer'], resident['id'], 'OBS-A-0102',
                  full_signals(False, False, False, False))
    score_resident(resident['id'])
    lead = token(TestClient(app), 'lead@test.local')
    with TestClient(app) as client:
        revoke = client.post(f"/observations/{high['id']}/revoke",
                             json={'reason': '高等级信号核实为误记，按剩余观察重算。'},
                             headers=auth(lead))
        assert revoke.status_code == 200
        # 默认观察列表隐藏已撤销记录。
        visible = client.get('/observations', headers=auth(lead)).json()
        assert all(item['status'] != 'revoked' for item in visible['items'])
        assert visible['revocations'] == []
        # 包含撤销留档时可查到原观察与撤销缘由。
        archive = client.get('/observations?include_revoked=true', headers=auth(lead)).json()
        revoked = next(item for item in archive['items'] if item['id'] == high['id'])
        assert revoked['status'] == 'revoked'
        assert revoked['revocation_reason'].startswith('高等级信号')
        assert revoked['revoked_by_name'] == '值班长乙'
        assert len(archive['revocations']) == 1
        # 告警详情同时给出剩余观察与撤销台账，等级已按剩余观察重算。
        detail = client.get(f"/alerts/{high['alert_id']}", headers=auth(lead)).json()
        assert detail['priority'] == 1
        assert detail['status'] == 'review'
        assert len(detail['revocations']) == 1
        assert detail['revocations'][0]['observation_ref'] == 'OBS-A-0101'
        statuses = {item['ref']: item['status'] for item in detail['observations']}
        assert statuses['OBS-A-0101'] == 'revoked'
        assert statuses['OBS-A-0102'] == 'scored'


def test_revoke_retry_over_http_does_not_duplicate_records(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-A-0201',
                  full_signals(True, True, False, False))
    lead = token(TestClient(app), 'lead@test.local')
    body = {'reason': '网络重试，同一条撤销请求需要幂等。'}
    with TestClient(app) as client:
        first = client.post(f"/observations/{row['id']}/revoke", json=body, headers=auth(lead))
        second = client.post(f"/observations/{row['id']}/revoke", json=body, headers=auth(lead))
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()['revocation']['id'] == second.json()['revocation']['id']
        with connect() as db:
            assert db.execute(
                "SELECT count(*) AS n FROM observation_revocations WHERE observation_id=%s",
                (row['id'],)).fetchone()['n'] == 1


def test_signed_alert_rejects_revocation_but_keeps_handover_snapshot(ids):
    staff, resident = ids
    row = observe(staff['carer'], resident['id'], 'OBS-A-0301',
                  full_signals(False, True, False, True))
    score_resident(resident['id'])
    with connect() as db:
        from src.schemas import Handover, Receipt, Review
        from src.workflow import dispatch, lock_alert, review, sign
        alert = lock_alert(db, row['alert_id'])
        review(db, alert['id'], Review(revision=alert['revision'], priority=alert['priority'],
                                       note='复核后交班。'), staff['lead'])
        alert = lock_alert(db, row['alert_id'])
        handover = dispatch(db, alert['id'], Handover(
            revision=alert['revision'], target_shift='night', next_action='接班核对。'), staff['lead'])
        sign(db, handover['id'], Receipt(receipt_note='已签收，按原件接续。'), staff['receiver'])
    lead = token(TestClient(app), 'lead@test.local')
    with TestClient(app) as client:
        resp = client.post(f"/observations/{row['id']}/revoke",
                           json={'reason': '签收后尝试撤销，应被拒绝。'}, headers=auth(lead))
        assert resp.status_code == 409
        handovers = client.get('/handovers', headers=auth(lead)).json()
        signed = next(item for item in handovers if item['id'] == handover['id'])
        assert signed['status'] == 'signed'
        assert [item['ref'] for item in signed['snapshot']['observations']] == ['OBS-A-0301']
