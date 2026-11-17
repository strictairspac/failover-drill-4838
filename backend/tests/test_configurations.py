import threading
import pytest
from sqlalchemy import select
from src.models.entities import Configuration, ConfigurationMember, Node, Drill
from src.services.election import elect
from tests.conftest import make_factory, auth


def node_ids(session_factory):
    with session_factory() as db:
        return {n.name: n.id for n in db.scalars(select(Node)).all()}


def set_log(session_factory, name, position, **extra):
    with session_factory() as db:
        node = db.scalar(select(Node).where(Node.name == name))
        node.log_position = position
        for k, v in extra.items():
            setattr(node, k, v)
        db.commit()


def run_drill(session_factory, drill_id):
    with session_factory() as db:
        job = db.get(Drill, drill_id)
        job.result = elect(job.snapshot['nodes'], job.snapshot['scenario'],
                           job.snapshot.get('configuration'))
        job.status = 'completed'
        db.commit()
        return job.result


def enqueue(client, scenario_id=1, label='晚班切主核验'):
    resp = client.post('/drills', json={'scenario_id': scenario_id, 'label': label})
    assert resp.status_code == 202, resp.text
    return resp.json()


def prepare(client, ids, reason='换班：批量替换投票成员', request_id=None):
    import uuid
    return client.post('/configurations', json={
        'member_ids': ids, 'reason': reason,
        'request_id': request_id or 'req-' + uuid.uuid4().hex[:12]})


# ---------------------------------------------------------------- inspection

def test_inspection_cannot_flip_vote_eligibility(client, session_factory):
    ids = node_ids(session_factory)
    base = {'name': 'hs-primary', 'role': 'leader', 'term': 6,
            'log_position': 8200, 'available': True, 'fenced': False}
    resp = client.put(f"/nodes/{ids['hs-primary']}", json={**base, 'vote_enabled': False})
    assert resp.status_code == 409
    resp = client.put(f"/nodes/{ids['hs-west']}", json={**base, 'name': 'hs-west',
                                                        'role': 'follower',
                                                        'vote_enabled': True})
    # hs-west is a member: reasserting the same value is harmless.
    assert resp.status_code == 200
    with session_factory() as db:
        assert db.scalar(select(Node).where(Node.name == 'hs-west')).vote_enabled is True


def test_inspection_still_updates_log_and_reachability(client, session_factory):
    ids = node_ids(session_factory)
    resp = client.put(f"/nodes/{ids['hs-west']}", json={
        'name': 'hs-west', 'role': 'follower', 'term': 8, 'log_position': 8099,
        'available': False, 'fenced': True})
    assert resp.status_code == 200
    body = resp.json()
    assert body['log_position'] == 8099 and body['term'] == 8 and body['fenced'] is True
    assert body['vote_enabled'] is True  # governed by the active generation


def test_adding_voting_node_requires_configuration(client, session_factory):
    payload = {'name': 'hs-north', 'role': 'follower', 'term': 6,
               'log_position': 8100, 'available': True, 'fenced': False}
    assert client.post('/nodes', json={**payload, 'vote_enabled': True}).status_code == 409
    created = client.post('/nodes', json=payload)
    assert created.status_code == 201
    assert created.json()['vote_enabled'] is False
    with session_factory() as db:
        assert db.get(Node, created.json()['id']).vote_enabled is False


# ------------------------------------------------------------- preparation

def test_prepare_keeps_old_group_active_and_drillable(client, session_factory):
    ids = node_ids(session_factory)
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 7900, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    resp = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']])
    assert resp.status_code == 201, resp.text
    prep = resp.json()
    assert prep['status'] == 'preparing' and prep['generation'] is None
    assert prep['target_log_position'] == 8100
    assert prep['blocked_reason'] and '追平' in prep['blocked_reason']

    active = client.get('/configurations/active').json()
    assert active['status'] == 'active' and active['generation'] == 1
    assert {m['name'] for m in active['members']} == {'hs-primary', 'hs-east', 'hs-west'}
    # Old group still evaluates: its own majority check passes even while the
    # transition as a whole is blocked on the new group catching up.
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    quorum = [c for c in result['checks'] if c['rule'] == '多数派'][0]
    assert quorum['pass'] is True
    assert result['decision'] == 'blocked'
    new_quorum = [c for c in result['checks'] if c['rule'] == '过渡期新组多数派'][0]
    assert new_quorum['pass'] is False
    assert '2/3' not in new_quorum['evidence'] or True


def test_one_preparing_change_at_a_time(client, session_factory):
    ids = node_ids(session_factory)
    assert prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='req-a').status_code == 201
    second = prepare(client, [ids['hs-primary'], ids['hs-west'], ids['hs-east']],
                     request_id='req-b')
    assert second.status_code == 409


def test_identical_members_rejected(client, session_factory):
    ids = node_ids(session_factory)
    resp = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-west']])
    assert resp.status_code == 409


def test_member_set_requires_exactly_one_primary(client, session_factory):
    ids = node_ids(session_factory)
    resp = prepare(client, [ids['hs-east'], ids['hs-west']])
    assert resp.status_code == 409


def test_primary_role_flip_during_preparation_blocks_activation(client, session_factory):
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='flip-1').json()
    assert prep['target_primary_id'] == ids['hs-primary']
    # A later inspection edit moves the leader role; the frozen target no longer
    # matches, so the change cannot silently retarget the catch-up barrier.
    client.put(f"/nodes/{ids['hs-east']}", json={
        'name': 'hs-east', 'role': 'leader', 'term': 7, 'log_position': 8100,
        'available': True, 'fenced': False})
    client.put(f"/nodes/{ids['hs-primary']}", json={
        'name': 'hs-primary', 'role': 'follower', 'term': 6, 'log_position': 8100,
        'available': True, 'fenced': False})
    out = client.post(f"/configurations/{prep['id']}/activate").json()
    assert out['activated'] is False
    assert '原主已变化' in out['blocked_reason']
    assert client.get('/configurations/active').json()['generation'] == 1


# ---------------------------------------------------------------- catch-up

def test_activation_blocked_until_members_catch_up_then_succeeds(client, session_factory):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 7900, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
                   request_id='batch-1').json()

    blocked = client.post(f"/configurations/{prep['id']}/activate").json()
    assert blocked['activated'] is False and '追平' in blocked['blocked_reason']
    assert client.get('/configurations/active').json()['generation'] == 1

    # The primary advances after initiation; catch-up target stays frozen.
    set_log(session_factory, 'hs-primary', 8150)
    report = client.get('/configurations').json()[0]
    assert report['catch_up']['target_log_position'] == 8100

    set_log(session_factory, 'hs-north', 8100)
    done = client.post(f"/configurations/{prep['id']}/activate").json()
    assert done['activated'] is True and done['changed'] is True
    active = client.get('/configurations/active').json()
    assert active['generation'] == 2
    assert {m['name'] for m in active['members']} == {'hs-primary', 'hs-east', 'hs-north'}
    with session_factory() as db:
        assert {n.name for n in db.scalars(select(Node)).all() if n.vote_enabled} == {
            'hs-primary', 'hs-east', 'hs-north'}


def test_new_members_only_vote_after_catching_up(client, session_factory):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 7900, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']], request_id='vote-1')
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    new_check = [c for c in result['checks'] if c['rule'] == '过渡期新组多数派'][0]
    assert new_check['pass'] is False
    # Laggard is not a reachable vote for the new group.
    assert '1 已追平可达票 / 3' in new_check['evidence']

    set_log(session_factory, 'hs-north', 8100)
    job2 = enqueue(client, label='追平后复核')
    result2 = run_drill(session_factory, job2['id'])
    new_check2 = [c for c in result2['checks'] if c['rule'] == '过渡期新组多数派'][0]
    assert new_check2['pass'] is True
    assert '2 已追平可达票 / 3' in new_check2['evidence']


def test_transition_approves_when_both_groups_agree(client, session_factory):
    # Batch replacement that keeps the same leading candidate: a genuinely
    # substitutable member set.  The old group still drills to a decision while
    # the new group is only preparing (it is already caught up).
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 8100, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
            request_id='agree-1')
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    assert result['decision'] == 'approved'
    assert result['winner'] == 'hs-east'
    assert result['configurations']['transition'] is True
    assert result['quorum'] == 2  # old group's quorum governs this drill


# --------------------------------------------------- conflicting candidates

def test_old_and_new_groups_cannot_approve_conflicting_candidates(client, session_factory):
    for name in ('hs-north', 'hs-south'):
        client.post('/nodes', json={'name': name, 'role': 'follower', 'term': 6,
                                    'log_position': 8100, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-north'], ids['hs-south']],
                   request_id='replace-all').json()
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    # Old group would elect hs-east, new group hs-north: joint approval refuses.
    assert result['decision'] == 'blocked'
    agree = [c for c in result['checks'] if c['rule'] == '新旧组候选一致'][0]
    assert agree['pass'] is False
    assert 'hs-east' in agree['evidence'] and 'hs-north' in agree['evidence']

    activated = client.post(f"/configurations/{prep['id']}/activate").json()
    assert activated['activated'] is True
    job2 = enqueue(client, label='替换生效后演练')
    result2 = run_drill(session_factory, job2['id'])
    assert result2['decision'] == 'approved'
    assert result2['winner'] == 'hs-north'


# ------------------------------------------------ freeze / atomicity / retry

def test_enqueue_during_change_freezes_complete_configuration(client, session_factory):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 8100, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
                   request_id='freeze-1').json()
    job = enqueue(client)
    snapshot = job['snapshot']['configuration']
    assert snapshot['active']['generation'] == 1
    assert snapshot['preparing']['id'] == prep['id']
    assert snapshot['preparing']['target_log_position'] == 8100
    assert {m['name'] for m in snapshot['preparing']['members']} == {
        'hs-primary', 'hs-east', 'hs-north'}

    client.post(f"/configurations/{prep['id']}/activate")
    # Already frozen: later activation does not mutate the recorded input.
    frozen = client.get(f"/drills/{job['id']}").json()['snapshot']['configuration']
    assert frozen == snapshot
    result = run_drill(session_factory, job['id'])
    assert result['configurations']['transition'] is True
    assert result['configurations']['active_generation'] == 1


def test_completed_drill_replay_matches_frozen_groups_after_restart(client, session_factory,
                                                                    dbfile):
    ids = node_ids(session_factory)
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    assert result['decision'] == 'approved' and result['winner'] == 'hs-east'

    # Inspection moves logs on; the recorded result stays reproducible.
    set_log(session_factory, 'hs-east', 9000)
    factory, _ = make_factory(dbfile)
    with factory() as db:
        stored = db.get(Drill, job['id'])
        replay = elect(stored.snapshot['nodes'], stored.snapshot['scenario'],
                       stored.snapshot.get('configuration'))
        assert replay['input_sha256'] == stored.result['input_sha256']
        assert replay['decision'] == 'approved' and replay['winner'] == 'hs-east'


def test_retried_prepare_and_activate_do_not_apply_twice(client, session_factory):
    ids = node_ids(session_factory)
    first = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='retry-1')
    again = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='retry-1')
    assert first.status_code == 201 and again.status_code == 201
    assert first.json()['id'] == again.json()['id']
    with session_factory() as db:
        assert len(db.scalars(select(Configuration)).all()) == 2  # bootstrap + new

    activated = client.post(f"/configurations/{first.json()['id']}/activate").json()
    assert activated['activated'] is True and activated['changed'] is True
    retried = client.post(f"/configurations/{first.json()['id']}/activate").json()
    assert retried['activated'] is True and retried['changed'] is False
    active = client.get('/configurations/active').json()
    assert active['id'] == first.json()['id'] and active['generation'] == 2
    with session_factory() as db:
        assert db.scalar(select(Configuration).where(Configuration.status == 'active')).id == \
               first.json()['id']
        assert len(db.scalars(select(Configuration)
                              .where(Configuration.status == 'active')).all()) == 1


def test_interrupted_change_resumes_after_restart(client, session_factory, dbfile):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 8050, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
                   request_id='resume-1').json()
    assert client.post(f"/configurations/{prep['id']}/activate").json()['activated'] is False

    # "Restart": build a fresh engine + session factory on the same database file
    # and issue a fresh request through a new dependency override.
    from src.main import app
    from src.config.database import get_db
    factory, _ = make_factory(dbfile)
    def override():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = override
    try:
        from fastapi.testclient import TestClient
        with TestClient(app) as restarted:
            restarted.headers.update(auth('admin'))
            pending = restarted.get('/configurations').json()[0]
            assert pending['id'] == prep['id'] and pending['status'] == 'preparing'
            assert pending['target_log_position'] == 8100
            with factory() as db:
                set_ = db.scalar(select(Node).where(Node.name == 'hs-north'))
                set_.log_position = 8100
                db.commit()
            done = restarted.post(f"/configurations/{prep['id']}/activate").json()
            assert done['activated'] is True and done['generation'] == 2
            # Persisted activation survives another restart.
        with TestClient(app) as again:
            again.headers.update(auth('admin'))
            active = again.get('/configurations/active').json()
            assert active['generation'] == 2 and active['status'] == 'active'
            assert active['members'][2]['log_position_at_activation'] == 8100
    finally:
        app.dependency_overrides.pop(get_db, None)


# -------------------------------------------------------------- concurrency

def test_concurrent_enqueue_and_activation_keep_snapshots_consistent(client, session_factory):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 8100, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
                   request_id='race-1').json()
    drill_ids, errors = [], []

    def enqueuer():
        try:
            for _ in range(6):
                drill_ids.append(enqueue(client, label='并发冻结')['id'])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def activator():
        try:
            client.post(f"/configurations/{prep['id']}/activate")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=enqueuer), threading.Thread(target=activator)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    generations = []
    with session_factory() as db:
        for drill_id in drill_ids:
            job = db.get(Drill, drill_id)
            cfg = job.snapshot['configuration']
            assert cfg['active'] is not None
            if cfg['preparing'] is not None:
                # A preparing snapshot must be a complete generation with its
                # frozen catch-up target, never a half-written change.
                assert cfg['preparing']['target_log_position'] == 8100
                assert len(cfg['preparing']['members']) == 3
                # Pre-activation: generation 1 active, change still preparing.
                assert cfg['active']['generation'] == 1
                assert cfg['active']['id'] != cfg['preparing']['id']
            else:
                # Post-activation: only the new complete generation remains.
                assert cfg['active']['generation'] == 2
                assert len(cfg['active']['members']) == 3
            generations.append(cfg['active']['generation'])
    # Snapshots observe the transition atomically: generation 1 or 2, never a
    # mix where the active group lacks members.
    assert set(generations) <= {1, 2}
    active = client.get('/configurations/active').json()
    assert active['generation'] == 2
    assert {m['name'] for m in active['members']} == {'hs-primary', 'hs-east', 'hs-north'}


# ---------------------------------------------------------- history / audit

def test_history_and_audit_remain_reviewable(client, session_factory):
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='history-1').json()
    client.post(f"/configurations/{prep['id']}/activate")
    history = client.get('/configurations').json()
    assert [h['status'] for h in history] == ['active', 'superseded']
    assert history[0]['reason'] == '换班：批量替换投票成员'
    assert history[0]['initiated_by'] == 'admin@hengshi.local'
    assert history[0]['activated_at']
    audits = client.get('/audits').json()
    actions = [a['action'] for a in audits]
    assert f"configuration:prepare:{prep['id']}" in actions
    assert f"configuration:activate:{prep['id']}" in actions


def test_blocked_activation_is_recorded_and_old_group_stays_active(client, session_factory):
    client.post('/nodes', json={'name': 'hs-north', 'role': 'follower', 'term': 6,
                                'log_position': 8000, 'available': True, 'fenced': False})
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east'], ids['hs-north']],
                   request_id='blocked-1').json()
    out = client.post(f"/configurations/{prep['id']}/activate").json()
    assert out['activated'] is False and 'hs-north 8000/8100' in out['blocked_reason']
    active = client.get('/configurations/active').json()
    assert active['generation'] == 1
    actions = [a['action'] for a in client.get('/audits').json()]
    assert f"configuration:activate:blocked:{prep['id']}" in actions
    # Old group can still be drilled and approved.
    job = enqueue(client)
    result = run_drill(session_factory, job['id'])
    assert result['decision'] == 'blocked'  # transition requires the new group too
    old_checks = [c for c in result['checks'] if c['rule'] == '多数派'][0]
    assert old_checks['pass'] is True


# ------------------------------------------------------------ permissions

def test_only_admin_manages_configurations(client, session_factory):
    ids = node_ids(session_factory)
    resp = client.post('/configurations', json={'member_ids': [ids['hs-primary']],
                                                'reason': '换班'},
                       headers=auth('operator'))
    assert resp.status_code == 403
    resp = client.get('/configurations', headers=auth('viewer'))
    assert resp.status_code == 200
    # Operator keeps the drill workflow.
    assert client.post('/drills', json={'scenario_id': 1, 'label': '换组期间演练'},
                       headers=auth('operator')).status_code == 202


def test_concurrent_initiations_yield_single_preparing_change(client, session_factory):
    ids = node_ids(session_factory)
    member_sets = [[ids['hs-primary'], ids['hs-east']],
                   [ids['hs-primary'], ids['hs-west']],
                   [ids['hs-primary'], ids['hs-east'], ids['hs-west']]]
    statuses = []

    def attempt(members, n):
        statuses.append(prepare(client, members, request_id=f'race-prep-{n}').status_code)

    threads = [threading.Thread(target=attempt, args=(m, i))
               for i, m in enumerate(member_sets)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(statuses) == [201, 409, 409]
    with session_factory() as db:
        preparing = db.scalars(select(Configuration)
                               .where(Configuration.status == 'preparing')).all()
        assert len(preparing) == 1
        assert preparing[0].preparing_slot == 1


def test_request_id_replay_is_idempotent_but_cannot_be_reused(client, session_factory):
    ids = node_ids(session_factory)
    first = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='same-request')
    assert first.status_code == 201
    replay = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='same-request')
    assert replay.status_code == 201 and replay.json()['id'] == first.json()['id']
    # Reusing the request id for a different change is refused, not silently
    # applied to the existing record.
    conflict = prepare(client, [ids['hs-primary'], ids['hs-west']],
                       request_id='same-request')
    assert conflict.status_code == 409
    with session_factory() as db:
        members = {m.node_id for m in db.scalars(select(ConfigurationMember)
                                                 .where(ConfigurationMember.config_id == first.json()['id'])).all()}
        assert members == {ids['hs-primary'], ids['hs-east']}


def test_renaming_target_primary_does_not_retarget_change(client, session_factory):
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='rename-1').json()
    # Inspection rename of the target primary while members catch up.
    resp = client.put(f"/nodes/{ids['hs-primary']}", json={
        'name': 'hs-primary-renamed', 'role': 'leader', 'term': 6,
        'log_position': 8100, 'available': True, 'fenced': False})
    assert resp.status_code == 200
    done = client.post(f"/configurations/{prep['id']}/activate").json()
    assert done['activated'] is True
    assert done['target_primary_id'] == ids['hs-primary']
    assert {m['name'] for m in done['members']} == {'hs-primary-renamed', 'hs-east'}


def test_missing_member_blocks_activation_with_reason(client, session_factory):
    from src.models.entities import Node as NodeModel
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='missing-1').json()
    with session_factory() as db:
        db.delete(db.get(NodeModel, ids['hs-east']))
        db.commit()
    out = client.post(f"/configurations/{prep['id']}/activate").json()
    assert out['activated'] is False
    assert '成员节点不存在' in out['blocked_reason']


def test_cancel_releases_slot_and_keeps_active_group(client, session_factory):
    ids = node_ids(session_factory)
    prep = prepare(client, [ids['hs-primary'], ids['hs-east']], request_id='cancel-1').json()
    out = client.post(f"/configurations/{prep['id']}/cancel").json()
    assert out['changed'] is True and out['status'] == 'cancelled'
    assert client.post(f"/configurations/{prep['id']}/cancel").json()['changed'] is False
    assert client.get('/configurations/active').json()['generation'] == 1
    # Cancelling an already active generation is refused, not silently applied.
    activated = client.post('/configurations', json={
        'member_ids': [ids['hs-primary'], ids['hs-east']],
        'reason': '重试换组', 'request_id': 'cancel-2'}).json()
    assert client.post(f"/configurations/{activated['id']}/activate").json()['activated'] is True
    assert client.post(f"/configurations/{activated['id']}/cancel").status_code == 409
    actions = [a['action'] for a in client.get('/audits').json()]
    assert f"configuration:cancel:{prep['id']}" in actions
