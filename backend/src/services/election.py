"""Deterministic election model. Never contacts or promotes production nodes.

Vote eligibility comes from the voter-group generations (组态) frozen into the
drill snapshot, never from ad-hoc node inspection edits.  While a new generation
is preparing, decisions require a majority of both the old and the new group
(joint consensus), and new members only count once they have caught up to the
original primary's log position recorded at change initiation.
"""
import hashlib, json


def _build_group(nodes, members, caught_up_target=None):
    """Resolve a frozen member list into voter/candidate facts."""
    by_id = {n['id']: n for n in nodes}
    resolved = [by_id[m['node_id']] for m in members if m['node_id'] in by_id]
    return {'members': resolved, 'configured_quorum': len(members) // 2 + 1,
            'caught_up_target': caught_up_target}


def _caught_up(node, target):
    return target is None or node['log_position'] >= target


def _group_decision(group, scenario, primary, latest, caught_up_target):
    members = group['members']
    quorum = group['configured_quorum']
    failed = scenario['fail_primary'] and primary is not None

    def voter(n):
        return (n['available'] and not n['fenced'] and _caught_up(n, caught_up_target)
                and not (failed and n['id'] == primary['id']))

    reachable = [n for n in members if voter(n)]
    candidates = []
    for n in members:
        lag = latest - n['log_position']
        eligible = (n['role'] == 'follower' and n['available'] and not n['fenced']
                    and _caught_up(n, caught_up_target) and lag <= scenario['max_data_loss'])
        candidates.append({'name': n['name'], 'node_id': n['id'], 'log_position': n['log_position'],
                           'lag': lag, 'eligible': eligible})
    qualified = sorted([c for c in candidates if c['eligible']],
                       key=lambda c: (-c['log_position'], c['name']))
    return {'reachable': reachable, 'reachable_votes': len(reachable), 'quorum': quorum,
            'configured_voters': len(members), 'majority': len(reachable) >= quorum,
            'candidates': candidates, 'winner': qualified[0]['name'] if qualified else None,
            'qualified': bool(qualified)}


def _legacy_group(nodes):
    members = [n for n in nodes if n['vote_enabled']]
    return {'members': members, 'configured_quorum': len(members) // 2 + 1, 'caught_up_target': None}


def elect(nodes, scenario, configuration=None):
    if not nodes: raise ValueError('至少需要一个节点')
    leaders = [n for n in nodes if n['role'] == 'leader']
    if len(leaders) != 1: raise ValueError('必须且只能配置一个原主节点')
    primary = leaders[0]
    latest = max(n['log_position'] for n in nodes)
    term = max(n['term'] for n in nodes) + 1
    fenced = primary['fenced'] or scenario['fence_primary']

    active_cfg = (configuration or {}).get('active')
    preparing_cfg = (configuration or {}).get('preparing')
    legacy = active_cfg is None
    if legacy:
        active_group = _legacy_group(nodes)
        if not active_group['members']:
            raise ValueError('至少需要一个有投票权节点')
        active_quorum = active_group['configured_quorum']
        old = _group_decision(active_group, scenario, primary, latest, None)
    else:
        active_group = _build_group(nodes, active_cfg['members'])
        active_quorum = active_group['configured_quorum']
        if not active_group['members']:
            raise ValueError('生效组态没有可用成员')
        old = _group_decision(active_group, scenario, primary, latest, None)

    checks = [
        {'rule': '故障切换触发', 'pass': scenario['fail_primary'] or not primary['available'],
         'evidence': f"fail_primary={scenario['fail_primary']}; available={primary['available']}"},
        {'rule': '多数派', 'pass': old['majority'],
         'evidence': f"{old['reachable_votes']} 可达票 / {old['configured_voters']} 配置票，需要 {active_quorum}"}]

    new = None
    new_group = None
    if preparing_cfg is not None:
        new_group = _build_group(nodes, preparing_cfg['members'], preparing_cfg['target_log_position'])
        new = _group_decision(new_group, scenario, primary, latest,
                              preparing_cfg['target_log_position'])
        caught = sum(1 for n in new_group['members']
                     if n['log_position'] >= preparing_cfg['target_log_position'])
        checks.append({
            'rule': '过渡期新组多数派',
            'pass': new['majority'],
            'evidence': (f"{new['reachable_votes']} 已追平可达票 / {new['configured_voters']} 新配置票，"
                         f"需要 {new['quorum']}；追平发起时原主日志 {preparing_cfg['target_log_position']} "
                         f"({caught}/{len(new_group['members'])} 已追平)")})
        agree = old['winner'] == new['winner'] and bool(old['winner'])
        checks.append({
            'rule': '新旧组候选一致',
            'pass': agree,
            'evidence': (f"旧组候选 {old['winner'] or '无'}；新组候选 {new['winner'] or '无'}。"
                         '过渡期须新旧组共同同意同一候选，禁止各自放行冲突候选')})

    checks.extend([
        {'rule': '旧主写入隔离 fencing', 'pass': fenced,
         'evidence': '已证明写入隔离' if fenced else '缺少 fencing 证明，阻止脑裂'},
        {'rule': '任期门槛', 'pass': term >= scenario['required_term'],
         'evidence': f"新任期 {term} / 要求 {scenario['required_term']}"},
        {'rule': 'RPO 合格候选', 'pass': old['qualified'],
         'evidence': f"{sum(1 for c in old['candidates'] if c['eligible'])} 个候选，允许丢失 {scenario['max_data_loss']} 条日志"}])

    approved = all(c['pass'] for c in checks)
    candidates = [{**c, 'configuration': 'active' if not legacy else 'legacy'} for c in old['candidates']]
    if new is not None:
        candidates += [{**c, 'configuration': 'preparing'} for c in new['candidates']]
    # Historical snapshots recorded before group generations existed carry no
    # configuration key; keep their digest identical so old records still replay.
    payload = {'nodes': nodes, 'scenario': scenario}
    if configuration is not None:
        payload['configuration'] = configuration
    source = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return {'decision': 'approved' if approved else 'blocked',
            'winner': old['winner'] if approved else None,
            'new_term': term, 'quorum': active_quorum,
            'reachable_votes': old['reachable_votes'], 'checks': checks, 'candidates': candidates,
            'configurations': {'active_generation': active_cfg and active_cfg.get('generation'),
                               'active_members': [m['name'] for m in active_cfg['members']] if active_cfg else None,
                               'preparing_generation': preparing_cfg and preparing_cfg.get('generation'),
                               'preparing_members': [m['name'] for m in preparing_cfg['members']] if preparing_cfg else None,
                               'preparing_target_log_position': preparing_cfg and preparing_cfg.get('target_log_position'),
                               'transition': preparing_cfg is not None},
            'input_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'model_version': 'quorum-fencing-rpo-v1' if legacy else 'quorum-fencing-rpo-v2-joint',
            'simulation': True,
            'boundary': '受控演练模型；未连接真实复制集群，未执行生产切主。任期、日志位置、fencing 由输入声明。'}
