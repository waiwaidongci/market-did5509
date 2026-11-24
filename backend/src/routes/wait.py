from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.schemas.requests import ManifestRequest, ShardsRequest
from src.models.wait import WaitMarketDay
from src.services import wait as service

router = APIRouter(prefix='/wait')


@router.get('/days')
def days(market: str | None = Query(None, max_length=300), limit: int = Query(200, ge=1, le=1000),
         db: Session = Depends(connection), actor: User = Depends(current)):
    return service.list_days(db, market, limit)


@router.get('/days/{day_id}')
def day(day_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    owner = db.get(WaitMarketDay, day_id)
    if not owner:
        raise HTTPException(404, '找不到该市场日')
    return service.day_detail(db, owner)


@router.post('/manifests', status_code=201)
def manifests(body: ManifestRequest, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        owner, manifest, created, adoption, status = service.submit_manifest(
            db, body.market, body.date, body.batch_no, body.expected_shards,
            body.total_trucks, actor.username)
    except service.WaitConflict as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    except Exception:
        db.rollback()
        raise
    return {'created': created, 'market_day': service.day_detail(db, owner),
            'batch': _batch_view(status)}


@router.post('/shards', status_code=201)
def shards(body: ShardsRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        owner, adoption, status, created = service.submit_shards(
            db, body.market, body.date, body.batch_no,
            [item.model_dump() for item in body.shards], actor.username)
    except service.WaitConflict as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    except Exception:
        db.rollback()
        raise
    detail = service.day_detail(db, owner)
    batch = next((item for item in detail['batches']
                  if item['batch_no'] == body.batch_no), None)
    return {'created_shards': len(created), 'market_day': detail, 'batch': batch}


def _batch_view(status):
    keys = ('batch_no', 'expected_shards', 'received_shards', 'missing_shards',
            'unexpected_shards', 'declared_trucks', 'observed_trucks', 'total_matches',
            'complete', 'adopted', 'p90_seconds', 'superseded', 'reason')
    return {key: status.get(key) for key in keys if key in status}
