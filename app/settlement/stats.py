from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload, selectinload

from app import models, schemas
from app.settlement.caliber import HOURS_PER_DAY
from app.settlement.engine import (
    UnitSettlementAccumulator,
    accumulate_grouped_by_unit,
)


DIMENSIONS = {"daily", "monthly", "batch"}

CURTAILMENT_REASON_LABELS = {
    models.CurtailmentRecord.REASON_BOOSTER_STATION: "升压站容量受限",
    models.CurtailmentRecord.REASON_TRANSMISSION_LINE: "送出线路容量受限",
    models.CurtailmentRecord.REASON_GRID_DISPATCH: "电网调度指令",
    models.CurtailmentRecord.REASON_EQUIPMENT_FAULT: "设备故障",
}

_REVIEW_STATUS_LABELS = {
    models.TrialOperationReview.STATUS_PENDING: "待复核",
    models.TrialOperationReview.STATUS_PASSED: "复核通过",
    models.TrialOperationReview.STATUS_REJECTED: "已驳回",
    models.TrialOperationReview.STATUS_RETURNED: "退回日报修正",
}

_APP_STATUS_LABELS = {
    models.GridAcceptance.APP_NOT_SUBMITTED: "未提交申请",
    models.GridAcceptance.APP_SUBMITTED: "已提交申请",
    models.GridAcceptance.APP_APPROVED: "申请已批准",
}

_ACCEPTANCE_RESULT_LABELS = {
    models.GridAcceptance.RESULT_PENDING: "待验收",
    models.GridAcceptance.RESULT_TRIAL_OPERATION: "试运行中",
    models.GridAcceptance.RESULT_PASSED: "验收通过",
    models.GridAcceptance.RESULT_FAILED: "验收未通过",
}


def curtailment_reason_label(reason_type: str) -> str:
    return CURTAILMENT_REASON_LABELS.get(reason_type, reason_type)


def _acceptance_status_summary(acc: Optional[models.GridAcceptance]) -> str:
    if acc is None:
        return "无验收记录"
    parts = [_APP_STATUS_LABELS.get(acc.application_status, acc.application_status)]
    if acc.protection_setting_verified:
        parts.append("保护定值已核对")
    if acc.dispatch_permission_no:
        parts.append(f"调度许可 {acc.dispatch_permission_no}")
    if acc.acceptance_result == models.GridAcceptance.RESULT_PASSED and acc.acceptance_date:
        parts.append(f"验收日期 {acc.acceptance_date.isoformat()}")
    return "；".join(parts)


def _load_reports(
    db: Session,
    start_date: Optional[date],
    end_date: Optional[date],
    batch: Optional[str],
    unit_id: Optional[int],
) -> List[models.DailyReport]:
    q = db.query(models.DailyReport).options(
        joinedload(models.DailyReport.unit).selectinload(models.Unit.acceptance),
        selectinload(models.DailyReport.trial_review),
        selectinload(models.DailyReport.curtailment_allocations),
    )
    if start_date:
        q = q.filter(models.DailyReport.report_date >= start_date)
    if end_date:
        q = q.filter(models.DailyReport.report_date <= end_date)
    if unit_id:
        q = q.filter(models.DailyReport.unit_id == unit_id)
    if batch:
        q = q.join(models.Unit).filter(models.Unit.batch == batch)
    return q.order_by(models.DailyReport.report_date, models.DailyReport.unit_id).all()


def _group_key(dimension: str, report: models.DailyReport) -> str:
    if dimension == "daily":
        return report.report_date.isoformat()
    if dimension == "monthly":
        return report.report_date.strftime("%Y-%m")
    if dimension == "batch":
        return report.unit.batch
    raise ValueError(f"不支持的统计维度: {dimension}")


def _calc_total_hours(
    reports: List[models.DailyReport],
    dimension: str,
    group_key: Optional[str] = None,
) -> float:
    """根据统计维度计算该组数据覆盖的总小时数（用于可用率计算）。"""
    if dimension == "daily":
        return HOURS_PER_DAY
    if dimension == "batch" and group_key is None:
        dates = {r.report_date for r in reports}
        return len(dates) * HOURS_PER_DAY
    if dimension == "monthly":
        dates = [r.report_date for r in reports]
        if dates:
            d0 = min(dates)
            from calendar import monthrange
            _, days = monthrange(d0.year, d0.month)
            return days * HOURS_PER_DAY
    dates = {r.report_date for r in reports}
    return max(1.0, len(dates) * HOURS_PER_DAY)


def _build_unit_stats_item(acc: UnitSettlementAccumulator, total_hours: float) -> schemas.UnitStatsItem:
    cap = acc.unit.rated_capacity_kw or 0.0
    equiv = acc.grid_connected_kwh / cap if cap else 0.0
    avail_rate = acc.available_hours / total_hours if total_hours > 0 else 0.0
    return schemas.UnitStatsItem(
        unit_id=acc.unit.id,
        unit_code=acc.unit.code,
        batch=acc.unit.batch,
        rated_capacity_kw=round(cap, 2),
        generation_kwh=round(acc.generation_kwh, 2),
        grid_connected_kwh=round(acc.grid_connected_kwh, 2),
        curtailed_kwh=round(acc.curtailed_kwh, 2),
        allocated_curtailed_kwh=round(acc.allocated_curtailed_kwh, 2),
        fault_downtime_hours=round(acc.fault_downtime_hours, 2),
        available_hours=round(acc.available_hours, 2),
        settlement_kwh=round(acc.settlement_kwh, 2),
        trial_operation_kwh=round(acc.trial_operation_kwh, 2),
        pending_review_kwh=round(acc.pending_review_kwh, 2),
        reviewed_settled_kwh=round(acc.reviewed_settled_kwh, 2),
        review_difference_kwh=round(acc.review_difference_kwh, 2),
        review_difference_notes=list(acc.review_difference_notes),
        equivalent_utilization_hours=round(equiv, 2),
        availability_rate=round(avail_rate, 4),
    )


def _build_group_stats_item(
    group_key: str,
    accs: Dict[int, UnitSettlementAccumulator],
    total_hours: float,
) -> schemas.StatsGroupItem:
    units = [_build_unit_stats_item(a, total_hours) for a in accs.values()]
    units.sort(key=lambda u: u.unit_code)
    total_cap = sum(u.rated_capacity_kw for u in units)
    total_grid = sum(u.grid_connected_kwh for u in units)
    total_avail_hours = sum(u.available_hours for u in units)
    equiv = total_grid / total_cap if total_cap else 0.0
    avail_rate = total_avail_hours / (total_hours * len(units)) if (total_hours * len(units)) > 0 else 0.0
    notes: List[str] = []
    for u in units:
        notes.extend(u.review_difference_notes)
    return schemas.StatsGroupItem(
        group_key=group_key,
        unit_count=len(units),
        rated_capacity_kw=round(total_cap, 2),
        generation_kwh=round(sum(u.generation_kwh for u in units), 2),
        grid_connected_kwh=round(total_grid, 2),
        curtailed_kwh=round(sum(u.curtailed_kwh for u in units), 2),
        allocated_curtailed_kwh=round(sum(u.allocated_curtailed_kwh for u in units), 2),
        fault_downtime_hours=round(sum(u.fault_downtime_hours for u in units), 2),
        available_hours=round(total_avail_hours, 2),
        settlement_kwh=round(sum(u.settlement_kwh for u in units), 2),
        trial_operation_kwh=round(sum(u.trial_operation_kwh for u in units), 2),
        pending_review_kwh=round(sum(u.pending_review_kwh for u in units), 2),
        reviewed_settled_kwh=round(sum(u.reviewed_settled_kwh for u in units), 2),
        review_difference_kwh=round(sum(u.review_difference_kwh for u in units), 2),
        review_difference_notes=notes,
        equivalent_utilization_hours=round(equiv, 2),
        availability_rate=round(avail_rate, 4),
        units=units,
    )


def build_statistics(
    db: Session,
    dimension: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
    unit_id: Optional[int] = None,
) -> schemas.StatisticsResponse:
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension 仅支持 {DIMENSIONS}")

    reports = _load_reports(db, start_date, end_date, batch, unit_id)

    grouped_reports: Dict[str, List[models.DailyReport]] = {}
    for r in reports:
        gk = _group_key(dimension, r)
        grouped_reports.setdefault(gk, []).append(r)

    group_items: List[schemas.StatsGroupItem] = []
    for gk, group_reports in grouped_reports.items():
        accs = accumulate_grouped_by_unit(group_reports, db)
        total_hours = _calc_total_hours(group_reports, dimension, gk)
        group_items.append(_build_group_stats_item(gk, accs, total_hours))
    group_items.sort(key=lambda g: g.group_key)

    total_hours_all = _calc_total_hours(reports, dimension, None)
    totals_accs = accumulate_grouped_by_unit(reports, db)
    totals = _build_group_stats_item("总计(全周期)", totals_accs, total_hours_all)

    return schemas.StatisticsResponse(
        dimension=dimension,
        start_date=start_date,
        end_date=end_date,
        batch=batch,
        totals=totals,
        groups=group_items,
    )


def build_settlement_report(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
) -> schemas.SettlementReport:
    reports = _load_reports(db, start_date, end_date, batch, None)
    accs = accumulate_grouped_by_unit(reports, db)

    rows: List[schemas.SettlementRow] = []
    for acc in accs.values():
        unit = acc.unit
        from app.settlement.deductions import unit_is_settled
        settled = unit_is_settled(unit)
        if not settled:
            excluded = "未通过并网验收，全部电量不计入结算"
        elif acc.pending_review_kwh > 0:
            excluded = (
                f"试运行待复核电量 {acc.pending_review_kwh:.1f} kWh 暂未转入结算，"
                f"其中复核通过 {acc.reviewed_settled_kwh:.1f} kWh 已计入结算"
            )
        else:
            excluded = None
        rows.append(
            schemas.SettlementRow(
                unit_id=unit.id,
                unit_code=unit.code,
                batch=unit.batch,
                rated_capacity_kw=round(unit.rated_capacity_kw, 2),
                grid_connected_kwh=round(acc.grid_connected_kwh, 2),
                settlement_kwh=round(acc.settlement_kwh, 2),
                trial_operation_kwh=round(acc.trial_operation_kwh, 2),
                pending_review_kwh=round(acc.pending_review_kwh, 2),
                reviewed_settled_kwh=round(acc.reviewed_settled_kwh, 2),
                review_difference_kwh=round(acc.review_difference_kwh, 2),
                review_difference_notes=list(acc.review_difference_notes),
                excluded_reason=excluded,
            )
        )
    rows.sort(key=lambda r: r.unit_code)

    return schemas.SettlementReport(
        start_date=start_date,
        end_date=end_date,
        total_settlement_kwh=round(sum(r.settlement_kwh for r in rows), 2),
        total_grid_connected_kwh=round(sum(r.grid_connected_kwh for r in rows), 2),
        total_trial_operation_kwh=round(sum(r.trial_operation_kwh for r in rows), 2),
        total_pending_review_kwh=round(sum(r.pending_review_kwh for r in rows), 2),
        total_reviewed_settled_kwh=round(sum(r.reviewed_settled_kwh for r in rows), 2),
        total_review_difference_kwh=round(sum(r.review_difference_kwh for r in rows), 2),
        rows=rows,
    )


def _load_reconciliation_reports(
    db: Session,
    start_date: Optional[date],
    end_date: Optional[date],
    batch: Optional[str],
    unit_id: Optional[int] = None,
) -> List[models.DailyReport]:
    q = db.query(models.DailyReport).options(
        selectinload(models.DailyReport.trial_review),
        selectinload(models.DailyReport.curtailment_allocations).selectinload(
            models.CurtailmentAllocation.curtailment_record
        ),
    )
    if start_date:
        q = q.filter(models.DailyReport.report_date >= start_date)
    if end_date:
        q = q.filter(models.DailyReport.report_date <= end_date)
    if unit_id:
        q = q.filter(models.DailyReport.unit_id == unit_id)
    if batch:
        q = q.join(models.Unit).filter(models.Unit.batch == batch)
    return q.order_by(models.DailyReport.report_date, models.DailyReport.unit_id).all()


def _deduction_to_schema(d: object) -> schemas.DeductionItem:
    return schemas.DeductionItem(
        type=getattr(d, "type", ""),
        label=getattr(d, "label", ""),
        kwh=round(getattr(d, "kwh", 0.0), 2),
        reason=getattr(d, "reason", None),
        daily_report_id=getattr(d, "daily_report_id", None),
        review_id=getattr(d, "review_id", None),
    )


def _build_unit_reconciliation_from_acc(
    acc: UnitSettlementAccumulator,
    reports: List[models.DailyReport],
) -> schemas.UnitReconciliationItem:
    unit = acc.unit
    cap = unit.rated_capacity_kw or 0.0
    from app.settlement.deductions import unit_is_settled
    acc_unit = unit
    settled = unit_is_settled(acc_unit)
    acceptance = acc_unit.acceptance

    acceptance_result = acceptance.acceptance_result if acceptance else models.GridAcceptance.RESULT_PENDING
    trial_hours = (acceptance.trial_operation_hours or 0.0) if acceptance else 0.0
    dispatch_no = acceptance.dispatch_permission_no if acceptance else None

    daily_refs: List[schemas.DailyReportRef] = []
    alloc_refs: List[schemas.CurtailmentAllocationRef] = []
    review_refs: List[schemas.TrialReviewRef] = []

    from app.settlement.caliber import normalize_daily_report_fields

    for report in reports:
        _, normalized_grid = normalize_daily_report_fields(report, cap)
        daily_refs.append(
            schemas.DailyReportRef(
                report_id=report.id,
                report_date=report.report_date,
                generation_kwh=round(report.generation_kwh, 2),
                grid_connected_kwh=round(normalized_grid, 2),
                curtailed_kwh=round(report.curtailed_kwh, 2),
                is_trial_operation=report.is_trial_operation,
                remark=report.remark,
            )
        )

        allocs = report.curtailment_allocations or []
        for a in allocs:
            cr = a.curtailment_record
            alloc_refs.append(
                schemas.CurtailmentAllocationRef(
                    allocation_id=a.id,
                    curtailment_record_id=a.curtailment_record_id,
                    record_date=cr.record_date if cr else report.report_date,
                    reason_type=cr.reason_type if cr else "",
                    reason_detail=cr.reason_detail if cr else None,
                    allocated_curtailed_kwh=round(a.allocated_curtailed_kwh, 2),
                    daily_report_id=a.daily_report_id,
                )
            )

        review = getattr(report, "trial_review", None)
        if report.is_trial_operation and review is not None:
            review_refs.append(
                schemas.TrialReviewRef(
                    review_id=review.id,
                    daily_report_id=review.daily_report_id,
                    review_date=review.review_date,
                    status=review.status,
                    review_kwh=round(review.review_kwh, 2),
                    settled_kwh=round(review.settled_kwh, 2),
                    difference_kwh=round(review.difference_kwh, 2),
                    difference_reason=review.difference_reason,
                    reviewer=review.reviewer,
                )
            )

    deduction_items = [_deduction_to_schema(d) for d in acc.deductions]

    return schemas.UnitReconciliationItem(
        unit_id=unit.id,
        unit_code=unit.code,
        unit_name=unit.name,
        batch=unit.batch,
        rated_capacity_kw=round(cap, 2),
        acceptance_status=_acceptance_status_summary(acceptance),
        acceptance_result=acceptance_result,
        dispatch_permission_no=dispatch_no,
        trial_operation_hours=round(trial_hours, 2),
        grid_connected_kwh=round(acc.grid_connected_kwh, 2),
        curtailed_kwh=round(acc.curtailed_kwh, 2),
        allocated_curtailed_kwh=round(acc.allocated_curtailed_kwh, 2),
        deduction_kwh=round(acc.deduction_kwh, 2),
        deduction_items=deduction_items,
        settlement_kwh=round(acc.settlement_kwh, 2),
        daily_report_refs=daily_refs,
        curtailment_allocation_refs=alloc_refs,
        trial_review_refs=review_refs,
    )


def _summarize_batch(
    batch: str,
    unit_items: List[schemas.UnitReconciliationItem],
) -> schemas.BatchReconciliationItem:
    reasons: set[str] = set()
    for u in unit_items:
        for ref in u.curtailment_allocation_refs:
            label = curtailment_reason_label(ref.reason_type)
            reasons.add(label if not ref.reason_detail else f"{label}（{ref.reason_detail}）")

    return schemas.BatchReconciliationItem(
        batch=batch,
        unit_count=len(unit_items),
        rated_capacity_kw=round(sum(u.rated_capacity_kw for u in unit_items), 2),
        trial_operation_hours=round(sum(u.trial_operation_hours for u in unit_items), 2),
        grid_connected_kwh=round(sum(u.grid_connected_kwh for u in unit_items), 2),
        curtailed_kwh=round(sum(u.curtailed_kwh for u in unit_items), 2),
        allocated_curtailed_kwh=round(sum(u.allocated_curtailed_kwh for u in unit_items), 2),
        deduction_kwh=round(sum(u.deduction_kwh for u in unit_items), 2),
        settlement_kwh=round(sum(u.settlement_kwh for u in unit_items), 2),
        curtailment_reasons=sorted(reasons),
        units=sorted(unit_items, key=lambda u: u.unit_code),
    )


def _load_units_for_reconciliation(
    db: Session, batch: Optional[str] = None
) -> List[models.Unit]:
    q = db.query(models.Unit).options(selectinload(models.Unit.acceptance))
    if batch:
        q = q.filter(models.Unit.batch == batch)
    return q.order_by(models.Unit.code).all()


def build_reconciliation_report(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
) -> schemas.ReconciliationReport:
    units = _load_units_for_reconciliation(db, batch)
    reports = _load_reconciliation_reports(db, start_date, end_date, batch)

    reports_by_unit: Dict[int, List[models.DailyReport]] = {}
    for r in reports:
        reports_by_unit.setdefault(r.unit_id, []).append(r)

    units_by_batch: Dict[str, List[models.Unit]] = {}
    for u in units:
        units_by_batch.setdefault(u.batch, []).append(u)

    batch_items: List[schemas.BatchReconciliationItem] = []
    for b, batch_units in units_by_batch.items():
        unit_items = []
        for u in batch_units:
            unit_reports = reports_by_unit.get(u.id, [])
            accs = accumulate_grouped_by_unit(unit_reports, db)
            acc = accs.get(u.id)
            if acc is None:
                from app.settlement.engine import UnitSettlementAccumulator
                acc = UnitSettlementAccumulator(unit=u)
            unit_items.append(_build_unit_reconciliation_from_acc(acc, unit_reports))
        batch_items.append(_summarize_batch(b, unit_items))
    batch_items.sort(key=lambda x: x.batch)

    all_unit_items = []
    for u in units:
        unit_reports = reports_by_unit.get(u.id, [])
        accs = accumulate_grouped_by_unit(unit_reports, db)
        acc = accs.get(u.id)
        if acc is None:
            from app.settlement.engine import UnitSettlementAccumulator
            acc = UnitSettlementAccumulator(unit=u)
        all_unit_items.append(_build_unit_reconciliation_from_acc(acc, unit_reports))

    totals = _summarize_batch("全部批次", all_unit_items)

    return schemas.ReconciliationReport(
        start_date=start_date,
        end_date=end_date,
        totals=totals,
        batches=batch_items,
    )


def build_batch_reconciliation_report(
    db: Session,
    batch: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> schemas.BatchReconciliationItem:
    units = _load_units_for_reconciliation(db, batch)
    reports = _load_reconciliation_reports(db, start_date, end_date, batch)
    reports_by_unit: Dict[int, List[models.DailyReport]] = {}
    for r in reports:
        reports_by_unit.setdefault(r.unit_id, []).append(r)

    unit_items = []
    for u in units:
        unit_reports = reports_by_unit.get(u.id, [])
        accs = accumulate_grouped_by_unit(unit_reports, db)
        acc = accs.get(u.id)
        if acc is None:
            from app.settlement.engine import UnitSettlementAccumulator
            acc = UnitSettlementAccumulator(unit=u)
        unit_items.append(_build_unit_reconciliation_from_acc(acc, unit_reports))

    return _summarize_batch(batch, unit_items)


def build_unit_reconciliation_report(
    db: Session,
    unit_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> schemas.UnitReconciliationItem:
    unit = (
        db.query(models.Unit)
        .options(selectinload(models.Unit.acceptance))
        .filter(models.Unit.id == unit_id)
        .first()
    )
    if unit is None:
        raise ValueError("机组不存在")
    reports = _load_reconciliation_reports(db, start_date, end_date, None, unit_id)
    accs = accumulate_grouped_by_unit(reports, db)
    acc = accs.get(unit_id)
    if acc is None:
        from app.settlement.engine import UnitSettlementAccumulator
        acc = UnitSettlementAccumulator(unit=unit)
    return _build_unit_reconciliation_from_acc(acc, reports)
