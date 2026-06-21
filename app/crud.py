from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session

from app import models, schemas, services


# ---------------- Units ----------------
def list_units(db: Session, batch: Optional[str] = None) -> List[models.Unit]:
    q = db.query(models.Unit)
    if batch:
        q = q.filter(models.Unit.batch == batch)
    return q.order_by(models.Unit.code).all()


def get_unit(db: Session, unit_id: int) -> Optional[models.Unit]:
    return db.get(models.Unit, unit_id)


def get_unit_by_code(db: Session, code: str) -> Optional[models.Unit]:
    return db.query(models.Unit).filter(models.Unit.code == code).first()


def create_unit(db: Session, payload: schemas.UnitCreate) -> models.Unit:
    unit = models.Unit(**payload.model_dump())
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


def delete_unit(db: Session, unit: models.Unit) -> None:
    db.delete(unit)
    db.commit()


# ---------------- GridAcceptance ----------------
def get_acceptance_by_unit(
    db: Session, unit_id: int
) -> Optional[models.GridAcceptance]:
    return (
        db.query(models.GridAcceptance)
        .filter(models.GridAcceptance.unit_id == unit_id)
        .first()
    )


def get_acceptance(db: Session, acceptance_id: int) -> Optional[models.GridAcceptance]:
    return db.get(models.GridAcceptance, acceptance_id)


def create_acceptance(
    db: Session, payload: schemas.GridAcceptanceCreate
) -> models.GridAcceptance:
    obj = models.GridAcceptance(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_acceptance(
    db: Session,
    obj: models.GridAcceptance,
    payload: schemas.GridAcceptanceUpdate,
) -> models.GridAcceptance:
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


# ---------------- DailyReport ----------------
def list_reports(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    unit_id: Optional[int] = None,
    batch: Optional[str] = None,
    trial: Optional[bool] = None,
) -> List[models.DailyReport]:
    q = db.query(models.DailyReport).join(models.Unit)
    if start_date:
        q = q.filter(models.DailyReport.report_date >= start_date)
    if end_date:
        q = q.filter(models.DailyReport.report_date <= end_date)
    if unit_id:
        q = q.filter(models.DailyReport.unit_id == unit_id)
    if batch:
        q = q.filter(models.Unit.batch == batch)
    if trial is not None:
        q = q.filter(models.DailyReport.is_trial_operation == trial)
    return q.order_by(models.DailyReport.report_date, models.Unit.code).all()


def get_report(db: Session, report_id: int) -> Optional[models.DailyReport]:
    return db.get(models.DailyReport, report_id)


def create_report(db: Session, payload: schemas.DailyReportCreate) -> models.DailyReport:
    unit = get_unit(db, payload.unit_id)
    cap = unit.rated_capacity_kw if unit else 0.0
    available, grid = services.normalize_daily_report_fields(payload, cap)
    data = payload.model_dump()
    data["available_hours"] = available
    data["grid_connected_kwh"] = grid
    obj = models.DailyReport(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_report(
    db: Session,
    obj: models.DailyReport,
    payload: schemas.DailyReportUpdate,
) -> models.DailyReport:
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    unit = get_unit(db, obj.unit_id)
    cap = unit.rated_capacity_kw if unit else 0.0
    available, grid = services.normalize_daily_report_fields(obj, cap)
    obj.available_hours = available
    obj.grid_connected_kwh = grid
    db.commit()
    db.refresh(obj)
    return obj


def delete_report(db: Session, obj: models.DailyReport) -> None:
    db.delete(obj)
    db.commit()


# ---------------- Curtailment ----------------
def list_curtailments(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[models.CurtailmentRecord]:
    q = db.query(models.CurtailmentRecord)
    if start_date:
        q = q.filter(models.CurtailmentRecord.record_date >= start_date)
    if end_date:
        q = q.filter(models.CurtailmentRecord.record_date <= end_date)
    return q.order_by(models.CurtailmentRecord.record_date.desc()).all()


def get_curtailment(db: Session, cid: int) -> Optional[models.CurtailmentRecord]:
    return db.get(models.CurtailmentRecord, cid)


def create_curtailment(
    db: Session, payload: schemas.CurtailmentRecordCreate
) -> models.CurtailmentRecord:
    obj = models.CurtailmentRecord(
        record_date=payload.record_date,
        reason_type=payload.reason_type,
        reason_detail=payload.reason_detail,
        total_curtailed_kwh=payload.total_curtailed_kwh,
    )
    for a in payload.allocations:
        obj.allocations.append(
            models.CurtailmentAllocation(
                unit_id=a.unit_id,
                daily_report_id=a.daily_report_id,
                allocated_curtailed_kwh=a.allocated_curtailed_kwh,
            )
        )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def delete_curtailment(db: Session, obj: models.CurtailmentRecord) -> None:
    db.delete(obj)
    db.commit()


# ---------------- TrialOperationReview ----------------
def list_reviews(
    db: Session,
    status: Optional[str] = None,
    unit_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[models.TrialOperationReview]:
    q = db.query(models.TrialOperationReview)
    if status:
        q = q.filter(models.TrialOperationReview.status == status)
    if unit_id:
        q = q.filter(models.TrialOperationReview.unit_id == unit_id)
    if start_date:
        q = q.filter(models.TrialOperationReview.review_date >= start_date)
    if end_date:
        q = q.filter(models.TrialOperationReview.review_date <= end_date)
    return q.order_by(models.TrialOperationReview.review_date.desc()).all()


def get_review(db: Session, review_id: int) -> Optional[models.TrialOperationReview]:
    return db.get(models.TrialOperationReview, review_id)


def get_review_by_report(
    db: Session, daily_report_id: int
) -> Optional[models.TrialOperationReview]:
    return (
        db.query(models.TrialOperationReview)
        .filter(models.TrialOperationReview.daily_report_id == daily_report_id)
        .first()
    )


def create_review(
    db: Session,
    report: models.DailyReport,
    reviewer: Optional[str] = None,
    review_note: Optional[str] = None,
) -> models.TrialOperationReview:
    obj = models.TrialOperationReview(
        daily_report_id=report.id,
        unit_id=report.unit_id,
        review_date=report.report_date,
        status=models.TrialOperationReview.STATUS_PENDING,
        review_kwh=report.grid_connected_kwh,
        settled_kwh=0.0,
        difference_kwh=0.0,
        reviewer=reviewer,
        review_note=review_note,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def save_review(
    db: Session, obj: models.TrialOperationReview
) -> models.TrialOperationReview:
    db.commit()
    db.refresh(obj)
    return obj
