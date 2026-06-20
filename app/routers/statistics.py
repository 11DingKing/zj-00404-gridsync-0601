from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import schemas, services
from app.database import get_db

router = APIRouter(prefix="/statistics", tags=["统计与结算"])


@router.get("/settlement", response_model=schemas.SettlementReport)
def settlement(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return services.build_settlement_report(db, start_date, end_date, batch)


@router.get("", response_model=schemas.StatisticsResponse)
def statistics(
    dimension: str = Query("daily", pattern="^(daily|monthly|batch)$"),
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
    unit_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    return services.build_statistics(
        db, dimension, start_date, end_date, batch, unit_id
    )
