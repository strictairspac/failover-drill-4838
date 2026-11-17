from sqlalchemy import select
from fastapi import HTTPException
from src.models.entities import Node
from src.services.configuration_service import active_member_ids
from src.utils.serialize import record


def list_nodes(db): return [record(n) for n in db.scalars(select(Node).order_by(Node.id))]


def _guard_vote_enabled(db, node, requested, creating):
    """Vote eligibility is owned by the active voter-group generation (组态).

    Inspectors may keep updating reachability, term, log position and fencing,
    but cannot flip a node in or out of the majority by editing the node row.
    """
    members = active_member_ids(db)
    if creating:
        # New nodes start outside every generation, even before one exists.
        if requested:
            raise HTTPException(409, '投票资格由生效组态决定，请通过组态变更流程加入新节点')
        node.vote_enabled = False
        return
    if requested is None:
        return
    if members is None:
        # No active generation yet (fresh install): inspection may not invent
        # one either.  The first group is created through the configuration API.
        if requested and not node.vote_enabled:
            raise HTTPException(409, '尚无生效组态，投票资格须由首次组态变更建立')
        return
    current = node.vote_enabled
    if requested != current:
        raise HTTPException(409, '投票资格由生效组态决定，节点巡检不能直接修改，请发起组态变更')
    node.vote_enabled = current


def save_node(db, payload, node_id=None):
    creating = node_id is None
    node = db.get(Node, node_id) if node_id else Node()
    if node is None: return None
    data = payload.model_dump()
    requested_vote = data.pop('vote_enabled', None)
    for k, v in data.items(): setattr(node, k, v)
    _guard_vote_enabled(db, node, requested_vote, creating)
    db.add(node); db.flush()
    return record(node)
