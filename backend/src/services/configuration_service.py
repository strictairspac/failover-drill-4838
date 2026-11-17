"""Voter-group generations (组态).

A configuration is the authoritative set of voting members.  A change is first
prepared as a new generation.  While it is preparing, the old generation stays
active and can still be drilled.  A new generation only takes effect after every
member has caught up to the original primary's log position frozen at
initiation.  Vote eligibility is never derived from inspection edits.

Concurrency and crash-safety notes
----------------------------------
* Initiating a change locks the base generation row and inserts a row into the
  unique ``preparing_slot`` column, so two concurrent initiations cannot both
  observe "no preparing change".  A crash before commit rolls back the whole
  preparation; after a restart the old generation is still active.
* Activation is a single transaction: member catch-up is re-checked, the new
  generation gets its number, the old one is superseded and the node
  ``vote_enabled`` projection is rewritten together.  Re-activating an already
  active generation is a no-op, so a retried request cannot burn a generation.
* Drill enqueue freezes nodes and the complete configuration state inside the
  same lock, so it never records a half-applied change.
"""
import threading
from contextlib import contextmanager
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.models.entities import Configuration, ConfigurationMember, Node, now
from src.utils.serialize import record

# In-process guard: one uvicorn process still serialises its writers even on
# databases that ignore SELECT ... FOR UPDATE (tests use SQLite).  In MySQL the
# row locks below are the cross-process guarantee.
_WRITE_LOCK = threading.RLock()


@contextmanager
def configuration_transaction(db):
    """Run a group-state mutation and its commit as one linearisable unit.

    The in-process guard must span the commit, otherwise a concurrent enqueue
    could freeze state between the service returning and the route committing.
    ``configuration_members`` is locked as well: activation rewrites member
    rows and a reader must never snapshot a half-applied generation.
    """
    with _WRITE_LOCK:
        db.scalars(select(Configuration).with_for_update()).all()
        db.scalars(select(ConfigurationMember).with_for_update()).all()
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise


# Read helpers stay lock-free for the GET endpoints.  Write paths first take
# ``configuration_transaction`` (a locking read of every generation and member
# row), which both serialises writers and establishes a MySQL read view that is
# at least as new as the last committed transition.
def active_configuration(db):
    return db.scalar(select(Configuration).where(Configuration.status == 'active')
                     .order_by(Configuration.generation.desc()).limit(1))


def preparing_configuration(db):
    return db.scalar(select(Configuration).where(Configuration.status == 'preparing')
                     .order_by(Configuration.id).limit(1))


def member_rows(db, config_id):
    return db.scalars(select(ConfigurationMember)
                      .where(ConfigurationMember.config_id == config_id)
                      .order_by(ConfigurationMember.id)).all()


def active_member_ids(db):
    active = active_configuration(db)
    if active is None:
        return None
    return {m.node_id for m in member_rows(db, active.id)}


def _member_dicts(db, config):
    if config is None:
        return []
    return [{'node_id': m.node_id, 'name': m.name} for m in member_rows(db, config.id)]


def _pack(config, db):
    if config is None:
        return None
    return {'id': config.id, 'generation': config.generation, 'status': config.status,
            'target_log_position': config.target_log_position,
            'target_primary_id': config.target_primary_id,
            'target_primary_name': config.target_primary_name,
            'members': _member_dicts(db, config)}


def config_snapshot(db):
    """A complete, self-consistent view frozen together with a drill input."""
    return {'active': _pack(active_configuration(db), db),
            'preparing': _pack(preparing_configuration(db), db),
            'frozen_at': now().isoformat()}


def catch_up_report(db, config):
    """Live readiness of a preparing generation, used for the blocked reason."""
    rows = member_rows(db, config.id)
    nodes = {n.id: n for n in db.scalars(select(Node)).all()}
    missing, pending, member_nodes = [], [], []
    caught = 0
    for member in rows:
        node = nodes.get(member.node_id)
        if node is None:
            missing.append(member.name)
            continue
        member_nodes.append(node)
        if node.log_position >= config.target_log_position:
            caught += 1
        else:
            pending.append(f'{node.name} {node.log_position}/{config.target_log_position}')
    reasons = []
    if missing:
        reasons.append('成员节点不存在：' + '、'.join(missing))
    leaders = [n for n in member_nodes if n.role == 'leader']
    if len(leaders) != 1:
        reasons.append('新组态必须且只能包含一个主节点')
    else:
        leader = leaders[0]
        # Compare identity, not name: renaming the node is an inspection edit
        # and must not silently retarget the catch-up barrier.
        if config.target_primary_id is not None and leader.id != config.target_primary_id:
            reasons.append('发起时原主已变化，请重新发起组态变更')
        if not leader.available:
            reasons.append(f'目标主节点 {leader.name} 不可达')
    if pending:
        reasons.append('等待成员追平发起时原主日志：' + '、'.join(pending))
    return {'ready': not reasons, 'reason': '；'.join(reasons) if reasons else None,
            'caught': caught, 'total': len(rows)}


def _guard_identical_members(db, active, member_ids):
    if active is None:
        return
    current = {m.node_id for m in member_rows(db, active.id)}
    if current == set(member_ids):
        raise HTTPException(409, '新组态与现行组态成员相同，无需发起变更')


def _by_request(db, request_id):
    return db.scalar(select(Configuration).where(Configuration.request_id == request_id))


def _replay_or_conflict(db, payload, existing):
    """Return an existing change for a replayed request, else reject reuse."""
    existing_members = {m.node_id for m in member_rows(db, existing.id)}
    if existing_members == set(payload.member_ids) and existing.reason == payload.reason:
        return existing, False
    raise HTTPException(409, '请求号已被另一组态变更使用，请使用新的请求号')


def create_configuration(db, payload, actor):
    """Prepare a new generation.  Returns (configuration, created)."""
    if payload.request_id:
        existing = _by_request(db, payload.request_id)
        if existing:
            return _replay_or_conflict(db, payload, existing)
    active = active_configuration(db)
    if active is None:
        raise HTTPException(409, '尚无生效组态，无法发起变更')
    base = db.get(Configuration, active.id, with_for_update=True)
    if preparing_configuration(db):
        raise HTTPException(409, '已有准备中的组态变更，请先生效或等待追平')
    nodes = {n.id: n for n in db.scalars(select(Node).with_for_update()).all()}
    member_nodes = []
    for node_id in payload.member_ids:
        node = nodes.get(node_id)
        if node is None:
            raise HTTPException(400, f'节点 {node_id} 不存在')
        member_nodes.append(node)
    _guard_identical_members(db, base, payload.member_ids)
    leaders = [n for n in member_nodes if n.role == 'leader']
    if len(leaders) != 1:
        raise HTTPException(409, '新组态必须且只能包含一个主节点')
    primary = leaders[0]
    global_leaders = db.scalars(select(Node).where(Node.role == 'leader')).all()
    if len(global_leaders) != 1:
        raise HTTPException(409, '当前必须且只能有一个主节点，请先修正节点角色')
    config = Configuration(status='preparing', preparing_slot=1, base_config_id=base.id,
                           target_log_position=primary.log_position,
                           target_primary_id=primary.id,
                           target_primary_name=primary.name,
                           initiated_by=actor, reason=payload.reason,
                           request_id=payload.request_id)
    try:
        with db.begin_nested():
            db.add(config)
            db.flush()
    except IntegrityError:
        existing = _by_request(db, payload.request_id) if payload.request_id else None
        if existing:
            return _replay_or_conflict(db, payload, existing)
        raise HTTPException(409, '已有准备中的组态变更，请先生效或等待追平')
    for node in member_nodes:
        db.add(ConfigurationMember(config_id=config.id, node_id=node.id, name=node.name))
    db.flush()
    return config, True


def activate_configuration(db, config_id, actor):
    """Activate a fully caught-up generation exactly once.

    Returns {'configuration', 'activated', 'changed', 'reason'}.  A retry of an
    already-active generation is a no-op; an interrupted attempt leaves the
    preparing row untouched, so it resumes from the same state.
    """
    config = db.get(Configuration, config_id, with_for_update=True)
    if config is None:
        raise HTTPException(404, '组态变更不存在')
    if config.status == 'active':
        return {'configuration': config, 'activated': True, 'changed': False, 'reason': None}
    if config.status != 'preparing':
        raise HTTPException(409, '该组态已失效，不能生效')
    # Lock the incumbent generation too, so enqueue cannot freeze between the
    # status updates below.
    active = active_configuration(db)
    if active is not None:
        active = db.get(Configuration, active.id, with_for_update=True)
    report = catch_up_report(db, config)
    if not report['ready']:
        config.blocked_reason = report['reason']
        db.flush()
        return {'configuration': config, 'activated': False, 'changed': False,
                'reason': report['reason']}
    generation = (db.scalar(select(func.max(Configuration.generation))) or 0) + 1
    config.generation = generation
    config.status = 'active'
    config.preparing_slot = None
    config.activated_at = now()
    config.blocked_reason = None
    if active is not None and active.id != config.id:
        active.status = 'superseded'
        active.preparing_slot = None
    members = member_rows(db, config.id)
    member_ids = {m.node_id for m in members}
    for node in db.scalars(select(Node).with_for_update()).all():
        node.vote_enabled = node.id in member_ids
    activated_at = config.activated_at
    for member in members:
        node = db.get(Node, member.node_id)
        member.log_position_at_activation = node.log_position if node else None
        member.caught_up_at = activated_at
    db.flush()
    return {'configuration': config, 'activated': True, 'changed': True, 'reason': None}


def cancel_configuration(db, config_id, actor):
    """Abandon a preparing change without touching the active generation.

    Used when the proposed group can no longer become ready (for example the
    original primary was lost mid-preparation).  Repeating the call is a no-op.
    """
    config = db.get(Configuration, config_id, with_for_update=True)
    if config is None:
        raise HTTPException(404, '组态变更不存在')
    if config.status == 'preparing':
        config.status = 'cancelled'
        config.preparing_slot = None
        config.blocked_reason = '发起人取消'
        db.flush()
        return config, True
    if config.status == 'cancelled':
        return config, False
    raise HTTPException(409, '只有准备中的组态变更可以取消')


def configuration_record(db, config):
    data = record(config)
    data.pop('preparing_slot', None)
    rows = member_rows(db, config.id)
    nodes = {n.id: n for n in db.scalars(select(Node)).all()}
    data['members'] = []
    for member in rows:
        node = nodes.get(member.node_id)
        data['members'].append({
            'node_id': member.node_id,
            # Live name for display; the prepare-time name stays in the record
            # so a later inspection rename cannot rewrite history.
            'name': node.name if node else member.name,
            'name_at_prepare': member.name,
            'current_log_position': node.log_position if node else None,
            'role': node.role if node else None,
            'available': node.available if node else None,
            'caught_up': bool(node) and node.log_position >= config.target_log_position,
            'log_position_at_activation': member.log_position_at_activation,
            'caught_up_at': member.caught_up_at.isoformat() if member.caught_up_at else None})
    report = catch_up_report(db, config)
    data['catch_up'] = {'target_log_position': config.target_log_position,
                        'caught': report['caught'], 'total': report['total']}
    if config.status == 'preparing':
        data['blocked_reason'] = report['reason']
    else:
        data['blocked_reason'] = config.blocked_reason
    return data


def list_configurations(db):
    configs = db.scalars(select(Configuration).order_by(Configuration.id.desc()).limit(100)).all()
    return [configuration_record(db, c) for c in configs]
