"""门岗分片入库、封账对账与九成秒数采用。

关键口径：
- 分片按 (市场日, 批号, 片号) 去重，同内容重试不重复计车，异内容冲突。
- 对账只看片号集合与封账总车数，与到达顺序无关。
- 只有应到片号齐备且总车数一致时才计算九成秒数，并在同一事务内切换采用结果。
- 采用批号只向前推进；旧批晚到补齐不会覆盖已采用的新批。
"""

from hashlib import sha256
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.domain.percentile import (
    merge_histograms,
    normalize_batch_no,
    normalize_buckets,
    normalize_date,
    normalize_market,
    normalize_shard_no,
    normalize_shards,
    normalize_total,
    percentile_detail,
    reconcile,
)
from src.models.wait import WaitAdoption, WaitManifest, WaitMarketDay, WaitShard
from src.services.audit import record
from src.utils.time import now


class WaitConflict(Exception):
    """同键异内容的重复上报。"""


def _signature(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256(raw.encode()).hexdigest()


def _lock_market_day(db, market, day):
    lookup = select(WaitMarketDay).where(WaitMarketDay.market == market, WaitMarketDay.date == day)
    item = db.scalar(lookup)
    if item is None:
        try:
            with db.begin_nested():
                item = WaitMarketDay(market=market, date=day)
                db.add(item)
                db.flush()
        except IntegrityError:
            item = db.scalar(lookup)
            if item is None:
                raise
    # 明确发出加锁读取，串行化同一市场日的分片与封账单，避免并发重复采用。
    locked = db.scalar(lookup.with_for_update())
    return locked or item


def register_manifest(db, market, day, batch_no, expected_shards, total_trucks, actor):
    market, day = normalize_market(market), normalize_date(day)
    batch_no = normalize_batch_no(batch_no)
    expected = normalize_shards(expected_shards)
    total = normalize_total(total_trucks)
    signature = _signature({'expected': expected, 'total': total})

    owner = _lock_market_day(db, market, day)
    lookup = select(WaitManifest).where(WaitManifest.market_day_id == owner.id,
                                        WaitManifest.batch_no == batch_no)
    existing = db.scalar(lookup)
    if existing is not None:
        if existing.checksum != signature:
            raise WaitConflict(f"批号 {batch_no} 的封账单已登记且内容不同，不能改写应到片号或总车数。")
        return owner, existing, False
    manifest = WaitManifest(market_day_id=owner.id, batch_no=batch_no, expected_shards=expected,
                            total_trucks=total, checksum=signature, author=actor)
    try:
        with db.begin_nested():
            db.add(manifest)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing is None:
            raise
        if existing.checksum != signature:
            raise WaitConflict(f"批号 {batch_no} 的封账单已登记且内容不同，不能改写应到片号或总车数。")
        return owner, existing, False
    record(db, actor, 'wait.manifest.received', owner.id, batch_no=batch_no,
           expected_shards=expected, total_trucks=total)
    return owner, manifest, True


def register_shard(db, owner, batch_no, shard_no, buckets, actor):
    batch_no = normalize_batch_no(batch_no)
    shard_no = normalize_shard_no(shard_no)
    normalized, truck_count = normalize_buckets(buckets)
    if truck_count <= 0:
        raise ValueError("分片至少需要一辆车。")
    signature = _signature({'shard': shard_no, 'buckets': normalized})

    lookup = select(WaitShard).where(WaitShard.market_day_id == owner.id,
                                     WaitShard.batch_no == batch_no, WaitShard.shard_no == shard_no)
    existing = db.scalar(lookup)
    if existing is not None:
        if existing.checksum != signature:
            raise WaitConflict(f"批号 {batch_no} 片号 {shard_no} 已上报且计数不同；重试请上报相同内容。")
        return existing, False
    shard = WaitShard(market_day_id=owner.id, batch_no=batch_no, shard_no=shard_no,
                      buckets=normalized, truck_count=truck_count, checksum=signature, author=actor)
    try:
        with db.begin_nested():
            db.add(shard)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing is None:
            raise
        if existing.checksum != signature:
            raise WaitConflict(f"批号 {batch_no} 片号 {shard_no} 已上报且计数不同；重试请上报相同内容。")
        return existing, False
    record(db, actor, 'wait.shard.received', owner.id, batch_no=batch_no, shard_no=shard_no,
           trucks=truck_count, buckets=len(normalized))
    return shard, True


def _adopt(db, owner, manifest, status, actor):
    """齐备后计算九成秒数并原子切换；旧批不得覆盖已采用的新批。"""
    current = db.scalar(select(WaitAdoption).where(WaitAdoption.market_day_id == owner.id)
                        .with_for_update())
    if current is not None and current.batch_no > manifest.batch_no:
        status['adopted'] = False
        status['superseded'] = True
        status['reason'] = f"批号 {manifest.batch_no} 晚到，已采用更新的批号 {current.batch_no}，不覆盖。"
        status['p90_seconds'] = current.p90_seconds
        record(db, actor, 'wait.batch.late', owner.id, batch_no=manifest.batch_no,
               adopted_batch_no=current.batch_no)
        return current, status
    if current is not None and current.batch_no == manifest.batch_no:
        # 同一批重复上报：结果不变，不刷新采用时间。
        status['adopted'] = True
        status['p90_seconds'] = current.p90_seconds
        return current, status

    # 只读取本批分片并分批合并，内存与秒数档位数相关，不随车辆数增长。
    stmt = (select(WaitShard.buckets)
            .where(WaitShard.market_day_id == owner.id, WaitShard.batch_no == manifest.batch_no)
            .order_by(WaitShard.shard_no)
            .execution_options(yield_per=500))
    histogram = merge_histograms(db.scalars(stmt))
    detail = percentile_detail(histogram, manifest.total_trucks)

    if current is None:
        current = WaitAdoption(market_day_id=owner.id)
        db.add(current)
    current.manifest_id = manifest.id
    current.batch_no = manifest.batch_no
    current.p90_seconds = detail['seconds']
    current.total_trucks = manifest.total_trucks
    current.shard_nos = status['received_shards']
    current.adopted_at = now()
    status['adopted'] = True
    status['p90_seconds'] = detail['seconds']
    record(db, actor, 'wait.batch.adopted', owner.id, batch_no=manifest.batch_no,
           p90_seconds=detail['seconds'], total_trucks=manifest.total_trucks,
           shard_nos=status['received_shards'])
    return current, status


def reconcile_batch(db, owner, manifest, actor):
    received = list(db.scalars(select(WaitShard.shard_no).where(
        WaitShard.market_day_id == owner.id, WaitShard.batch_no == manifest.batch_no).order_by(WaitShard.shard_no)))
    observed = db.scalar(select(func.coalesce(func.sum(WaitShard.truck_count), 0)).where(
        WaitShard.market_day_id == owner.id, WaitShard.batch_no == manifest.batch_no))
    status = reconcile(manifest.expected_shards, received, manifest.total_trucks, int(observed or 0))
    status.update(batch_no=manifest.batch_no, manifest_id=manifest.id, adopted=False)
    if not status['complete']:
        return None, status
    adoption, status = _adopt(db, owner, manifest, status, actor)
    return adoption, status


def submit_manifest(db, market, day, batch_no, expected_shards, total_trucks, actor):
    owner, manifest, created = register_manifest(db, market, day, batch_no, expected_shards, total_trucks, actor)
    adoption, status = reconcile_batch(db, owner, manifest, actor)
    db.commit()
    return owner, manifest, created, adoption, status


def submit_shards(db, market, day, batch_no, shards, actor):
    """单条或一批分片；整批同一事务，完成后只对账一次。"""
    owner = _lock_market_day(db, normalize_market(market), normalize_date(day))
    batch_no = normalize_batch_no(batch_no)
    if not isinstance(shards, (list, tuple)) or not shards:
        raise ValueError("至少需要一条分片。")
    created_ids = []
    for index, item in enumerate(shards, start=1):
        if not isinstance(item, dict) or 'shard_no' not in item or 'buckets' not in item:
            raise ValueError(f"第 {index} 条分片须包含 shard_no 与 buckets。")
        shard, created = register_shard(db, owner, batch_no, item['shard_no'], item['buckets'], actor)
        if created:
            created_ids.append(shard.id)
    manifest = db.scalar(select(WaitManifest).where(WaitManifest.market_day_id == owner.id,
                                                   WaitManifest.batch_no == batch_no))
    adoption, status = (None, None)
    if manifest is not None:
        adoption, status = reconcile_batch(db, owner, manifest, actor)
    db.commit()
    return owner, adoption, status, created_ids


def day_summary(db, owner, adoption=None):
    if adoption is None:
        adoption = db.scalar(select(WaitAdoption).where(WaitAdoption.market_day_id == owner.id))
    manifest = db.get(WaitManifest, adoption.manifest_id) if adoption else None
    data = {
        'id': owner.id, 'market': owner.market, 'date': owner.date,
        'adopted': None,
    }
    if adoption and manifest:
        data['adopted'] = {
            'batch_no': adoption.batch_no, 'manifest_id': manifest.id,
            'total_trucks': adoption.total_trucks, 'p90_seconds': adoption.p90_seconds,
            'p90_minutes': round(adoption.p90_seconds / 60, 2),
            'shard_nos': adoption.shard_nos, 'expected_shards': manifest.expected_shards,
            'manifest_checksum': manifest.checksum,
            'adopted_at': adoption.adopted_at.isoformat(timespec='seconds') + 'Z',
        }
    return data


def list_days(db, market=None, limit=200):
    query = select(WaitMarketDay, WaitAdoption).outerjoin(
        WaitAdoption, WaitAdoption.market_day_id == WaitMarketDay.id)
    if market:
        query = query.where(WaitMarketDay.market == normalize_market(market))
    query = query.order_by(WaitMarketDay.date.desc(), WaitMarketDay.market).limit(max(1, min(limit, 1000)))
    rows = db.execute(query).all()
    ids = [day.id for day, _ in rows]
    manifests, aggregates = {}, {}
    if ids:
        for manifest in db.scalars(select(WaitManifest).where(WaitManifest.market_day_id.in_(ids))):
            manifests.setdefault(manifest.market_day_id, {})[manifest.batch_no] = manifest
        for day_id, batch_no, count, trucks in db.execute(
                select(WaitShard.market_day_id, WaitShard.batch_no, func.count(), func.sum(WaitShard.truck_count))
                .where(WaitShard.market_day_id.in_(ids))
                .group_by(WaitShard.market_day_id, WaitShard.batch_no)):
            aggregates.setdefault(day_id, {})[batch_no] = (int(count or 0), int(trucks or 0))

    result = []
    for day, adoption in rows:
        item = day_summary(db, day, adoption)
        sealed = manifests.get(day.id, {})
        arrived = aggregates.get(day.id, {})
        pending = 0
        for batch_no, manifest in sealed.items():
            count, trucks = arrived.get(batch_no, (0, 0))
            if count != len(manifest.expected_shards) or trucks != manifest.total_trucks:
                pending += 1
        pending += len([batch for batch in arrived if batch not in sealed])
        item.update(manifest_count=len(sealed),
                    shard_count=sum(count for count, _ in arrived.values()),
                    pending_batches=pending)
        result.append(item)
    return result


def day_detail(db, owner):
    adoption = db.scalar(select(WaitAdoption).where(WaitAdoption.market_day_id == owner.id))
    data = day_summary(db, owner, adoption)
    manifests = list(db.scalars(select(WaitManifest).where(WaitManifest.market_day_id == owner.id)
                                .order_by(WaitManifest.batch_no.desc())))
    shards = db.execute(select(WaitShard.batch_no, WaitShard.shard_no, WaitShard.truck_count)
                        .where(WaitShard.market_day_id == owner.id)
                        .order_by(WaitShard.batch_no, WaitShard.shard_no)).all()
    by_batch = {}
    for batch_no, shard_no, trucks in shards:
        entry = by_batch.setdefault(batch_no, {'shard_nos': [], 'trucks': 0})
        entry['shard_nos'].append(shard_no)
        entry['trucks'] += trucks

    batches = []
    for manifest in manifests:
        entry = by_batch.pop(manifest.batch_no, {'shard_nos': [], 'trucks': 0})
        status = reconcile(manifest.expected_shards, entry['shard_nos'],
                           manifest.total_trucks, entry['trucks'])
        is_adopted = adoption is not None and adoption.manifest_id == manifest.id
        if is_adopted:
            label = '已采用'
        elif status['complete'] and adoption is not None and adoption.batch_no > manifest.batch_no:
            label = '晚到未采用'
        elif status['complete']:
            label = '齐备待采用'
        else:
            label = '待补齐'
        batches.append({
            'batch_no': manifest.batch_no, 'manifest_id': manifest.id,
            'expected_shards': manifest.expected_shards, 'total_trucks': manifest.total_trucks,
            'received_shards': status['received_shards'], 'observed_trucks': status['observed_trucks'],
            'missing_shards': status['missing_shards'], 'unexpected_shards': status['unexpected_shards'],
            'total_matches': status['total_matches'], 'complete': status['complete'],
            'adopted': is_adopted, 'status': label,
            'reason': status['pending_reason'], 'author': manifest.author,
            'created_at': manifest.created_at.isoformat(timespec='seconds') + 'Z',
        })
    for batch_no, entry in sorted(by_batch.items(), reverse=True):
        batches.append({
            'batch_no': batch_no, 'manifest_id': None, 'expected_shards': [],
            'total_trucks': None, 'received_shards': entry['shard_nos'],
            'observed_trucks': entry['trucks'], 'missing_shards': [], 'unexpected_shards': [],
            'total_matches': None, 'complete': False, 'adopted': False,
            'status': '等待封账单', 'reason': '尚未收到该批号的封账单。', 'author': None,
            'created_at': None,
        })
    data['batches'] = batches
    return data
