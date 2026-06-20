from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/reports", tags=["日报"])


@router.get("", response_model=List[schemas.DailyReportOut])
def list_reports(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    unit_id: Optional[int] = None,
    batch: Optional[str] = None,
    trial: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    return crud.list_reports(db, start_date, end_date, unit_id, batch, trial)


@router.post(
    "", response_model=schemas.DailyReportOut, status_code=status.HTTP_201_CREATED
)
def create_report(payload: schemas.DailyReportCreate, db: Session = Depends(get_db)):
    if not crud.get_unit(db, payload.unit_id):
        raise HTTPException(status_code=404, detail="机组不存在")
    return crud.create_report(db, payload)


@router.get("/{report_id}", response_model=schemas.DailyReportOut)
def get_report(report_id: int, db: Session = Depends(get_db)):
    obj = crud.get_report(db, report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="日报不存在")
    return obj


@router.patch("/{report_id}", response_model=schemas.DailyReportOut)
def update_report(
    report_id: int,
    payload: schemas.DailyReportUpdate,
    db: Session = Depends(get_db),
):
    obj = crud.get_report(db, report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="日报不存在")
    return crud.update_report(db, obj, payload)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(report_id: int, db: Session = Depends(get_db)):
    obj = crud.get_report(db, report_id)
    if not obj:
        raise HTTPException(status_code=404, detail="日报不存在")
    crud.delete_report(db, obj)
