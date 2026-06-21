from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud, models, schemas, services
from app.database import get_db

router = APIRouter(prefix="/reviews", tags=["试运行扣减复核"])


@router.get("", response_model=List[schemas.TrialOperationReviewOut])
def list_reviews(
    status_filter: Optional[str] = Query(None, alias="status"),
    unit_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    return crud.list_reviews(db, status_filter, unit_id, start_date, end_date)


@router.get("/pending", response_model=List[schemas.TrialOperationReviewOut])
def list_pending_reviews(db: Session = Depends(get_db)):
    return crud.list_reviews(
        db, status=models.TrialOperationReview.STATUS_PENDING
    )


@router.post(
    "",
    response_model=schemas.TrialOperationReviewOut,
    status_code=status.HTTP_201_CREATED,
)
def create_review(
    payload: schemas.TrialOperationReviewCreate, db: Session = Depends(get_db)
):
    report = crud.get_report(db, payload.daily_report_id)
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    if not report.is_trial_operation:
        raise HTTPException(status_code=400, detail="该日报非试运行日报，无需复核")
    if crud.get_review_by_report(db, payload.daily_report_id):
        raise HTTPException(status_code=400, detail="该日报已存在复核记录")
    return crud.create_review(db, report, payload.reviewer, payload.review_note)


@router.get("/{review_id}", response_model=schemas.TrialOperationReviewOut)
def get_review(review_id: int, db: Session = Depends(get_db)):
    obj = crud.get_review(db, review_id)
    if not obj:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    return obj


@router.post("/{review_id}/pass", response_model=schemas.TrialOperationReviewOut)
def pass_review(
    review_id: int,
    payload: schemas.TrialOperationReviewPass,
    db: Session = Depends(get_db),
):
    obj = crud.get_review(db, review_id)
    if not obj:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    if obj.status == models.TrialOperationReview.STATUS_PASSED:
        raise HTTPException(status_code=400, detail="该复核已通过，请勿重复操作")
    try:
        return services.pass_review(db, obj, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{review_id}/reject", response_model=schemas.TrialOperationReviewOut)
def reject_review(
    review_id: int,
    payload: schemas.TrialOperationReviewReject,
    db: Session = Depends(get_db),
):
    obj = crud.get_review(db, review_id)
    if not obj:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    if obj.status in (
        models.TrialOperationReview.STATUS_REJECTED,
        models.TrialOperationReview.STATUS_RETURNED,
    ):
        raise HTTPException(status_code=400, detail="该复核已驳回，请先修正日报后重新复核")
    try:
        return services.reject_review(db, obj, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{review_id}/reopen", response_model=schemas.TrialOperationReviewOut)
def reopen_review(review_id: int, db: Session = Depends(get_db)):
    obj = crud.get_review(db, review_id)
    if not obj:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    if obj.status not in (
        models.TrialOperationReview.STATUS_REJECTED,
        models.TrialOperationReview.STATUS_RETURNED,
    ):
        raise HTTPException(status_code=400, detail="仅驳回/退回状态的复核可重新发起")
    try:
        return services.reopen_review(db, obj)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
