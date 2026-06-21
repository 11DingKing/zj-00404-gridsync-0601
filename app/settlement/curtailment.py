from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app import models
from app.settlement.caliber import normalize_daily_report_fields


def get_curtailment_allocations_for_report(
    db: Session, report: models.DailyReport
) -> List[models.CurtailmentAllocation]:
    """获取某日报关联的所有限发分摊记录。"""
    return (
        db.query(models.CurtailmentAllocation)
        .filter(models.CurtailmentAllocation.daily_report_id == report.id)
        .all()
    )


def get_curtailment_allocations_for_date(
    db: Session, unit_id: int, record_date: date
) -> List[models.CurtailmentAllocation]:
    """获取某机组某天所有限发分摊记录（通过日报关联）。"""
    return (
        db.query(models.CurtailmentAllocation)
        .join(models.DailyReport)
        .filter(
            models.CurtailmentAllocation.unit_id == unit_id,
            models.DailyReport.report_date == record_date,
        )
        .all()
    )


def sum_allocated_curtailed_for_report(
    db: Session, report: models.DailyReport
) -> float:
    """汇总某日报关联的所有限发分摊电量（权威口径）。"""
    allocs = get_curtailment_allocations_for_report(db, report)
    return round(sum(a.allocated_curtailed_kwh for a in allocs), 2)


def get_authoritative_curtailed_kwh(
    report: models.DailyReport,
    db: Optional[Session] = None,
) -> float:
    """
    获取权威限电量：优先使用分摊记录汇总，无分摊则使用日报自报。
    取两者较大值，确保不会遗漏。
    """
    report_curt = getattr(report, "curtailed_kwh", 0.0) or 0.0
    allocated = 0.0

    if db is not None:
        allocated = sum_allocated_curtailed_for_report(db, report)
    elif hasattr(report, "curtailment_allocations"):
        allocated = round(
            sum(a.allocated_curtailed_kwh for a in report.curtailment_allocations), 2
        )

    return max(report_curt, allocated)


def validate_curtailment_allocations(
    db: Session,
    record_date: date,
    allocations: List[object],
    total_curtailed_kwh: float,
) -> List[str]:
    """
    校验限发分摊：
      1. 分摊电量之和 = 总限电量
      2. 每台机组当天必须有日报
      3. 分摊电量 ≤ 该机组当天「剩余可用容量」（= 容量 × 可用小时 − 实际发电），
         保证不把故障停机时段的电量也算作限电损失
      4. 同一机组同一天不重复分摊（检查是否已存在其他分摊记录）
    """
    from app import crud, schemas
    from app.settlement.caliber import validate_daily_report_capacity

    errors: List[str] = []

    alloc_sum = round(sum(a.allocated_curtailed_kwh for a in allocations), 2)
    if abs(alloc_sum - total_curtailed_kwh) > 0.01:
        errors.append(
            f"分摊电量之和 {alloc_sum} kWh 与总限电量 {total_curtailed_kwh} kWh 不一致"
        )

    seen_units: set[int] = set()
    for a in allocations:
        if a.unit_id in seen_units:
            errors.append(f"机组 {a.unit_id} 在本次分摊中重复出现")
            continue
        seen_units.add(a.unit_id)

        report = None
        if a.daily_report_id:
            report = crud.get_report(db, a.daily_report_id)
            if report and report.unit_id != a.unit_id:
                errors.append(
                    f"机组 {a.unit_id} 的分摊关联了错误的日报 {a.daily_report_id}"
                )
                report = None
        if report is None:
            report = (
                db.query(models.DailyReport)
                .filter(
                    models.DailyReport.unit_id == a.unit_id,
                    models.DailyReport.report_date == record_date,
                )
                .first()
            )
            if report is None:
                errors.append(f"机组 {a.unit_id} 在 {record_date} 没有日报，无法分摊")
                continue

        if report.report_date != record_date:
            errors.append(
                f"机组 {a.unit_id} 的日报日期 {report.report_date} 与限发日期 {record_date} 不一致"
            )

        unit = crud.get_unit(db, a.unit_id)
        if unit is None:
            errors.append(f"机组 {a.unit_id} 不存在")
            continue

        cap_errors = validate_daily_report_capacity(report, unit.rated_capacity_kw)
        if cap_errors:
            errors.extend(cap_errors)

        available, _ = normalize_daily_report_fields(report, unit.rated_capacity_kw)
        theoretical_max = round(unit.rated_capacity_kw * available, 2)
        remaining_capacity = max(0.0, theoretical_max - report.generation_kwh)

        existing_alloc = sum_allocated_curtailed_for_report(db, report)
        total_allocatable = max(0.0, remaining_capacity - existing_alloc)
        if a.allocated_curtailed_kwh > total_allocatable + 0.01:
            errors.append(
                f"机组 {unit.code} 在 {record_date} 最多可分摊限电 "
                f"{total_allocatable} kWh（剩余容量 {remaining_capacity} kWh"
                f" − 已分摊 {existing_alloc} kWh），本次分摊 {a.allocated_curtailed_kwh} kWh 超出"
            )

    return errors


def allocate_curtailed_by_available_hours(
    db: Session,
    record_date: date,
    total_curtailed_kwh: float,
    unit_ids: List[int],
) -> List[object]:
    """
    按各机组当天「可用小时 × 额定容量」的权重来分摊总限电量。
    故障停机时段不计入分摊权重，避免把故障损失也算作限电损失。
    """
    from app import crud, schemas

    weight_sum = 0.0
    unit_weights: Dict[int, Tuple[float, Optional[int]]] = {}

    for uid in unit_ids:
        unit = crud.get_unit(db, uid)
        if unit is None:
            continue
        report = (
            db.query(models.DailyReport)
            .filter(
                models.DailyReport.unit_id == uid,
                models.DailyReport.report_date == record_date,
            )
            .first()
        )
        if report is None:
            continue
        available, _ = normalize_daily_report_fields(report, unit.rated_capacity_kw)
        weight = max(0.0, unit.rated_capacity_kw * available)
        weight_sum += weight
        unit_weights[uid] = (weight, report.id)

    if weight_sum <= 0:
        return []

    result: List[schemas.CurtailmentAllocationCreate] = []
    remaining_kwh = total_curtailed_kwh
    uids = sorted(unit_weights.keys())
    for i, uid in enumerate(uids):
        weight, report_id = unit_weights[uid]
        if i == len(uids) - 1:
            alloc_kwh = round(remaining_kwh, 2)
        else:
            alloc_kwh = round(total_curtailed_kwh * weight / weight_sum, 2)
            remaining_kwh = round(remaining_kwh - alloc_kwh, 2)
        if alloc_kwh > 0:
            result.append(
                schemas.CurtailmentAllocationCreate(
                    unit_id=uid,
                    daily_report_id=report_id,
                    allocated_curtailed_kwh=alloc_kwh,
                )
            )
    return result
