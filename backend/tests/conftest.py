import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.database import Base, get_db
from src.main import app
from src.models.entities import User, Node, Scenario, Configuration, ConfigurationMember
from src.utils.security import hash_password, token
from scripts.seed_demo import bootstrap_configuration


@pytest.fixture()
def dbfile(tmp_path):
    return str(tmp_path / 'test.db')


def make_factory(path):
    engine = create_engine(f'sqlite:///{path}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


@pytest.fixture()
def session_factory(dbfile):
    factory, _ = make_factory(dbfile)
    with factory() as db:
        for role in ('admin', 'operator', 'viewer'):
            db.add(User(email=role + '@hengshi.local', role=role,
                        password_hash=hash_password('Drill2026!')))
        db.add_all([
            Node(name='hs-primary', role='leader', term=6, log_position=8100,
                 available=True, fenced=False, vote_enabled=True),
            Node(name='hs-east', role='follower', term=6, log_position=8100,
                 available=True, fenced=False, vote_enabled=True),
            Node(name='hs-west', role='follower', term=6, log_position=8098,
                 available=True, fenced=False, vote_enabled=True),
            Scenario(name='晚班隔离演练', fail_primary=True, fence_primary=True,
                     max_data_loss=0, required_term=7),
            Scenario(name='缺少隔离证明', fail_primary=True, fence_primary=False,
                     max_data_loss=0, required_term=7),
        ])
        db.flush()
        bootstrap_configuration(db)
        db.commit()
    return factory


@pytest.fixture()
def client(session_factory):
    def override():
        with session_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        c.headers.update({'Authorization': 'Bearer ' + token(type('U', (), {'email': 'admin@hengshi.local', 'role': 'admin'}))})
        yield c
    app.dependency_overrides.clear()


def auth(role):
    return {'Authorization': 'Bearer ' + token(type('U', (), {'email': role + '@hengshi.local', 'role': role}))}
