from datetime import datetime, timezone
from sqlalchemy import String, Integer, Boolean, JSON, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

def now(): return datetime.now(timezone.utc).replace(tzinfo=None)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(250))

class Node(Base):
    __tablename__ = 'nodes'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    term: Mapped[int] = mapped_column(Integer)
    log_position: Mapped[int] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean)
    fenced: Mapped[bool] = mapped_column(Boolean)
    # Legacy projection of active membership. Inspectors may not edit it directly.
    vote_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

class Configuration(Base):
    """An immutable-once-active voter group generation (组态)."""
    __tablename__ = 'configurations'
    id: Mapped[int] = mapped_column(primary_key=True)
    # Assigned only at activation so a retried activation cannot burn generations.
    generation: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='preparing', index=True)
    # Database-level guarantee that at most one generation is preparing at a
    # time; cleared (NULL) on every terminal transition.
    preparing_slot: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    base_config_id: Mapped[int | None] = mapped_column(ForeignKey('configurations.id'), nullable=True)
    # Frozen log position of the original primary at change initiation.
    target_log_position: Mapped[int] = mapped_column(Integer, default=0)
    target_primary_id: Mapped[int | None] = mapped_column(ForeignKey('nodes.id'), nullable=True)
    target_primary_name: Mapped[str] = mapped_column(String(80), default='')
    initiated_by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(200), default='')
    request_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    initiated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class ConfigurationMember(Base):
    __tablename__ = 'configuration_members'
    __table_args__ = (UniqueConstraint('config_id', 'node_id', name='uq_config_member'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    config_id: Mapped[int] = mapped_column(ForeignKey('configurations.id'), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey('nodes.id'))
    name: Mapped[str] = mapped_column(String(80))
    log_position_at_activation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caught_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class Scenario(Base):
    __tablename__ = 'scenarios'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    fail_primary: Mapped[bool] = mapped_column(Boolean)
    fence_primary: Mapped[bool] = mapped_column(Boolean)
    max_data_loss: Mapped[int] = mapped_column(Integer)
    required_term: Mapped[int] = mapped_column(Integer)

class Drill(Base):
    __tablename__ = 'drills'
    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey('scenarios.id'))
    label: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Audit(Base):
    __tablename__ = 'audits'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
