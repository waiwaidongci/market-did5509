import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.domain.wait_demo import demo_wait_days
from src.models.identity import User
from src.models.source import Dataset
from src.services import wait as wait_service
from src.services.datasets import save
from src.utils.security import hash_password


def _seed_wait(db):
    """可安全重复执行：同批同片同内容视为已存在，不重复计车。

    直接调用不提交的事务内服务函数，由外层 ``Session.begin()`` 统一提交，
    避免与账号/批次初始化混用提交造成部分回滚。
    """
    for day in demo_wait_days():
        try:
            owner, manifest, _ = wait_service.register_manifest(
                db, day['market'], day['date'], day['batch_no'],
                day['expected_shards'], day['total_trucks'], 'seed')
        except wait_service.WaitConflict:
            continue
        for shard in day['shards']:
            wait_service.register_shard(db, owner, day['batch_no'], shard['shard_no'],
                                        shard['buckets'], 'seed')
        wait_service.reconcile_batch(db, owner, manifest, 'seed')


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        _seed_wait(db)
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
