from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud, schemas, services
from app.database import get_db

router = APIRouter(prefix="/curtailments", tags=["限发"])


@router.get("", response_model=List[schemas.CurtailmentRecordOut])
def list_curtailments(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return crud.list_curtailments(db, start_date, end_date)


@router.get("/preview-allocations", response_model=List[schemas.CurtailmentAllocationCreate])
def preview_allocations(
    record_date: date,
    total_curtailed_kwh: float = Query(..., gt=0),
    unit_ids: List[int] = Query(...),
    db: Session = Depends(get_db),
):
    """
    按「可用小时 × 额定容量」的权重自动分摊总限电量。
    故障停机时段不计入权重，避免把故障损失重复算为限电损失。
    """
    for uid in unit_ids:
        if not crud.get_unit(db, uid):
            raise HTTPException(status_code=404, detail=f"机组 {uid} 不存在")
    result = services.allocate_curtailed_by_available_hours(
        db, record_date, total_curtailed_kwh, unit_ids
    )
    if not result:
        raise HTTPException(status_code=400, detail="参与分摊的机组当天均无可发电量（全部故障停机）")
    return result


@router.post(
    "",
    response_model=schemas.CurtailmentRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_curtailment(
    payload: schemas.CurtailmentRecordCreate, db: Session = Depends(get_db)
):
    for a in payload.allocations:
        if not crud.get_unit(db, a.unit_id):
            raise HTTPException(
                status_code=404, detail=f"机组 {a.unit_id} 不存在"
            )
    if payload.total_curtailed_kwh <= 0 and payload.allocations:
        raise HTTPException(status_code=400, detail="总限电量需大于0")
    errors = services.validate_curtailment_allocations(
        db, payload.record_date, payload.allocations, payload.total_curtailed_kwh
    )
    if errors:
        raise HTTPException(status_code=400, detail="；".join(errors))
    return crud.create_curtailment(db, payload)


@router.get("/{cid}", response_model=schemas.CurtailmentRecordOut)
def get_curtailment(cid: int, db: Session = Depends(get_db)):
    obj = crud.get_curtailment(db, cid)
    if not obj:
        raise HTTPException(status_code=404, detail="限发记录不存在")
    return obj


@router.delete("/{cid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_curtailment(cid: int, db: Session = Depends(get_db)):
    obj = crud.get_curtailment(db, cid)
    if not obj:
        raise HTTPException(status_code=404, detail="限发记录不存在")
    crud.delete_curtailment(db, obj)
