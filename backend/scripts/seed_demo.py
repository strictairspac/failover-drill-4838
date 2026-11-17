import os
from sqlalchemy import select
from src.config.database import Base, engine, Session
from src.models.entities import User, Node, Scenario, Configuration, ConfigurationMember, now
from src.utils.security import hash_password


def bootstrap_configuration(db):
    """Give legacy databases an explicit generation-1 voter group.

    Before group generations existed, ``nodes.vote_enabled`` was the only
    source of vote eligibility.  That initial set becomes generation 1 so every
    later change goes through the prepare/activate flow.
    """
    if db.scalar(select(Configuration.id).limit(1)) is not None:
        return
    nodes = db.scalars(select(Node).order_by(Node.id)).all()
    voters = [n for n in nodes if n.vote_enabled]
    if not voters:
        return
    leaders = [n for n in voters if n.role == 'leader']
    primary = leaders[0] if leaders else voters[0]
    # A legacy database may have several leaders; bootstrap records one target
    # primary while leaving the member set untouched.
    config = Configuration(generation=1, status='active', preparing_slot=None,
                           target_log_position=primary.log_position,
                           target_primary_id=primary.id,
                           target_primary_name=primary.name,
                           initiated_by='system:bootstrap', reason='初始组态引导',
                           activated_at=now())
    db.add(config); db.flush()
    for node in voters:
        db.add(ConfigurationMember(config_id=config.id, node_id=node.id, name=node.name,
                                   log_position_at_activation=node.log_position,
                                   caught_up_at=config.activated_at))
    for node in nodes:
        node.vote_enabled = node in voters
    db.flush()


def seed():
    Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO','true').lower() != 'true':
        with Session() as db:
            bootstrap_configuration(db); db.commit()
        return
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email = role + '@hengshi.local'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, role=role, password_hash=hash_password('Drill2026!')))
        for name, role, pos in [('hs-primary','leader',8100),('hs-east','follower',8100),('hs-west','follower',8098)]:
            if not db.scalar(select(Node).where(Node.name == name)):
                db.add(Node(name=name, role=role, term=6, log_position=pos, available=True, fenced=False, vote_enabled=True))
        for name, fence in [('晚班隔离演练',True),('缺少隔离证明',False)]:
            if not db.scalar(select(Scenario).where(Scenario.name == name)):
                db.add(Scenario(name=name, fail_primary=True, fence_primary=fence, max_data_loss=0, required_term=7))
        db.flush()
        bootstrap_configuration(db)
        db.commit()
if __name__ == '__main__': seed()
