from datetime import datetime
from sqlalchemy import JSON, BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class WaitMarketDay(Base):
    """市场日锚点：同一市场日的上报按此行串行对账。"""

    __tablename__ = 'wait_market_days'
    __table_args__ = (UniqueConstraint('market', 'date'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(300))
    date: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(default=now)


class WaitManifest(Base):
    """封账单：声明应到片号与总车数。同一批号内容不可改。"""

    __tablename__ = 'wait_manifests'
    __table_args__ = (UniqueConstraint('market_day_id', 'batch_no'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_day_id: Mapped[int] = mapped_column(ForeignKey('wait_market_days.id'))
    batch_no: Mapped[int] = mapped_column(BigInteger)
    expected_shards: Mapped[list] = mapped_column(JSON)
    total_trucks: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class WaitShard(Base):
    """门岗分片：等候秒数 → 车辆数，不保存单车记录。"""

    __tablename__ = 'wait_shards'
    __table_args__ = (UniqueConstraint('market_day_id', 'batch_no', 'shard_no'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_day_id: Mapped[int] = mapped_column(ForeignKey('wait_market_days.id'))
    batch_no: Mapped[int] = mapped_column(BigInteger)
    shard_no: Mapped[int] = mapped_column(BigInteger)
    buckets: Mapped[list] = mapped_column(JSON)
    truck_count: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class WaitAdoption(Base):
    """市场日当前采用的封账单与九成秒数，齐备后一次性切换。"""

    __tablename__ = 'wait_adoptions'

    id: Mapped[int] = mapped_column(primary_key=True)
    market_day_id: Mapped[int] = mapped_column(ForeignKey('wait_market_days.id'), unique=True)
    manifest_id: Mapped[int] = mapped_column(ForeignKey('wait_manifests.id'))
    batch_no: Mapped[int] = mapped_column(BigInteger)
    p90_seconds: Mapped[int] = mapped_column(BigInteger)
    total_trucks: Mapped[int] = mapped_column(BigInteger)
    shard_nos: Mapped[list] = mapped_column(JSON)
    adopted_at: Mapped[datetime] = mapped_column(default=now)
