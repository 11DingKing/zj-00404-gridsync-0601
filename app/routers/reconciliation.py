from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas, services
from app.database import get_db

router = APIRouter(prefix="/reconciliation", tags=["并网批次对账单"])


@router.get("", response_model=schemas.ReconciliationReport, summary="并网批次对账单汇总")
def reconciliation_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    按首批/后续批次汇总机组验收状态、试运行小时、上网电量、限发原因、扣减项与最终可结算电量。

    层级跳转：
      - totals（结算汇总，全部批次）→ batches[].units[].unit_id 可下钻到机组详情
      - batches[].batch 可下钻到单批次对账 GET /reconciliation/batches/{batch}
      - 机组详情 GET /reconciliation/units/{unit_id} 可追溯到日报 /reports/{report_id}、
        限发分摊依据 /curtailments/{curtailment_record_id}、试运行复核 /reviews/{review_id}
    """
    return services.build_reconciliation_report(db, start_date, end_date, batch)


@router.get(
    "/batches/{batch}",
    response_model=schemas.BatchReconciliationItem,
    summary="单批次对账明细",
)
def batch_reconciliation(
    batch: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """
    单个批次（如 首批 / 后续批次）的对账明细，含该批次下每台机组的扣减项与可追溯引用。
    batch 名称需 URL 编码，例如 GET /reconciliation/batches/%E9%A6%96%E6%89%B9
    """
    return services.build_batch_reconciliation_report(
        db, batch, start_date, end_date
    )


@router.get(
    "/units/{unit_id}",
    response_model=schemas.UnitReconciliationItem,
    summary="单机组对账详情",
)
def unit_reconciliation(
    unit_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """
    单台机组对账详情：验收状态、试运行小时、上网电量、限发原因、扣减项明细、最终可结算电量，
    并附带日报来源（daily_report_refs）、限发分摊依据（curtailment_allocation_refs）、
    试运行复核（trial_review_refs），可跳转到对应业务详情。
    """
    try:
        return services.build_unit_reconciliation_report(
            db, unit_id, start_date, end_date
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
