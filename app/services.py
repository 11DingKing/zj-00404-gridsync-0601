from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload, selectinload

from app import crud, models, schemas
from app.settlement.caliber import (
    HOURS_PER_DAY,
    normalize_daily_report_fields,
    validate_daily_report_capacity,
)
from app.settlement.curtailment import (
    allocate_curtailed_by_available_hours,
    get_curtailment_allocations_for_date,
    get_curtailment_allocations_for_report,
    sum_allocated_curtailed_for_report,
    validate_curtailment_allocations,
)
from app.settlement.deductions import unit_is_settled
from app.settlement.engine import (
    UnitSettlementAccumulator,
    accumulate_grouped_by_unit,
    calculate_daily_settlement,
)
from app.settlement import stats as _settlement_stats

DIMENSIONS = _settlement_stats.DIMENSIONS
HOURS_PER_DAY = HOURS_PER_DAY
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


# ---------------- 日报数据校验与规范化 ----------------
# 已迁移至 app.settlement.caliber，此处保留引用以兼容旧代码


# ---------------- 限发分摊校验与计算 ----------------
# 已迁移至 app.settlement.curtailment，此处保留引用以兼容旧代码


# ---------------- 统计与结算（统一口径） ----------------

class _UnitAccum:
    """累加单个机组在某统计区间内的电量指标。
    
    已重构为基于统一结算口径（UnitSettlementAccumulator），
    此处保留旧接口以兼容代码。
    """

    def __init__(self, unit: models.Unit) -> None:
        self._acc = UnitSettlementAccumulator(unit=unit)
        self.unit = unit

    def add(self, report: models.DailyReport, db: Optional[Session] = None) -> None:
        result = calculate_daily_settlement(report, self.unit, db)
        self._acc.add(result)

    @property
    def generation_kwh(self) -> float:
        return self._acc.generation_kwh

    @property
    def grid_connected_kwh(self) -> float:
        return self._acc.grid_connected_kwh

    @property
    def curtailed_kwh(self) -> float:
        return self._acc.curtailed_kwh

    @property
    def allocated_curtailed_kwh(self) -> float:
        return self._acc.allocated_curtailed_kwh

    @property
    def fault_downtime_hours(self) -> float:
        return self._acc.fault_downtime_hours

    @property
    def available_hours(self) -> float:
        return self._acc.available_hours

    @property
    def settlement_kwh(self) -> float:
        return self._acc.settlement_kwh

    @property
    def trial_operation_kwh(self) -> float:
        return self._acc.trial_operation_kwh

    @property
    def pending_review_kwh(self) -> float:
        return self._acc.pending_review_kwh

    @property
    def reviewed_settled_kwh(self) -> float:
        return self._acc.reviewed_settled_kwh

    @property
    def review_difference_kwh(self) -> float:
        return self._acc.review_difference_kwh

    @property
    def review_difference_notes(self) -> List[str]:
        return list(self._acc.review_difference_notes)


def _unit_settled(unit: models.Unit) -> bool:
    return unit_is_settled(unit)


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


def _build_unit_item(acc: _UnitAccum, total_hours: float) -> schemas.UnitStatsItem:
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


def _build_group_item(
    group_key: str,
    accs: Dict[int, _UnitAccum],
    total_hours: float,
) -> schemas.StatsGroupItem:
    units = [_build_unit_item(a, total_hours) for a in accs.values()]
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


def _accumulate_all(
    reports: List[models.DailyReport],
    db: Optional[Session] = None,
) -> Dict[int, _UnitAccum]:
    accs: Dict[int, _UnitAccum] = {}
    for r in reports:
        accs.setdefault(r.unit_id, _UnitAccum(r.unit)).add(r, db)
    return accs


def _accumulate_grouped(
    reports: List[models.DailyReport],
    dimension: str,
    db: Optional[Session] = None,
) -> Dict[str, Dict[int, _UnitAccum]]:
    grouped: Dict[str, Dict[int, _UnitAccum]] = {}
    for r in reports:
        gk = _group_key(dimension, r)
        grouped.setdefault(gk, {}).setdefault(r.unit_id, _UnitAccum(r.unit)).add(r, db)
    return grouped


def build_statistics(
    db: Session,
    dimension: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
    unit_id: Optional[int] = None,
) -> schemas.StatisticsResponse:
    return _settlement_stats.build_statistics(
        db, dimension, start_date, end_date, batch, unit_id
    )


def build_settlement_report(
    db: Session,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    batch: Optional[str] = None,
) -> schemas.SettlementReport:
    return _settlement_stats.build_settlement_report(db, start_date, end_date, batch)


# ---------------- 试运行扣减复核 ----------------

def _check_review_consistency(
    unit: models.Unit, report: models.DailyReport
) -> List[str]:
    """核对调度许可、验收结论、日报数据三者是否一致，返回不一致原因列表。"""
    reasons: List[str] = []
    acc = unit.acceptance
    if acc is None:
        reasons.append("机组无并网验收记录")
        return reasons
    if not acc.dispatch_permission_no:
        reasons.append("未取得调度许可")
    if acc.acceptance_result != models.GridAcceptance.RESULT_PASSED:
        reasons.append(f"验收结论为 {acc.acceptance_result}，尚未通过")
    if not report.is_trial_operation:
        reasons.append("日报未标记为试运行")
    if (
        acc.trial_operation_start_date
        and report.report_date < acc.trial_operation_start_date
    ):
        reasons.append("日报日期早于试运行开始日期")
    if (
        acc.trial_operation_end_date
        and report.report_date > acc.trial_operation_end_date
    ):
        reasons.append("日报日期晚于试运行结束日期")
    return reasons


def pass_review(
    db: Session,
    review: models.TrialOperationReview,
    payload: schemas.TrialOperationReviewPass,
) -> models.TrialOperationReview:
    report = (
        db.query(models.DailyReport)
        .options(joinedload(models.DailyReport.unit).selectinload(models.Unit.acceptance))
        .get(review.daily_report_id)
    )
    if report is None:
        raise ValueError("关联日报不存在")
    if not report.is_trial_operation:
        raise ValueError("该日报非试运行日报，无需复核")

    reasons = _check_review_consistency(report.unit, report)
    if reasons:
        raise ValueError("复核不通过：" + "；".join(reasons))

    settled = (
        payload.settled_kwh
        if payload.settled_kwh is not None
        else review.review_kwh
    )
    diff = round(review.review_kwh - settled, 2)
    if diff != 0 and not payload.difference_reason:
        raise ValueError("复核电量存在差异，须填写差异说明")

    acc = report.unit.acceptance
    review.status = models.TrialOperationReview.STATUS_PASSED
    review.settled_kwh = settled
    review.difference_kwh = diff
    review.difference_reason = payload.difference_reason
    review.dispatch_permission_no = acc.dispatch_permission_no if acc else None
    review.acceptance_result_snapshot = (
        acc.acceptance_result if acc else None
    )
    review.reviewer = payload.reviewer
    review.review_note = payload.review_note
    review.reviewed_at = date.today()
    return crud.save_review(db, review)


def reject_review(
    db: Session,
    review: models.TrialOperationReview,
    payload: schemas.TrialOperationReviewReject,
) -> models.TrialOperationReview:
    report = (
        db.query(models.DailyReport)
        .options(joinedload(models.DailyReport.unit).selectinload(models.Unit.acceptance))
        .get(review.daily_report_id)
    )
    reasons = _check_review_consistency(report.unit, report) if report else ["关联日报不存在"]
    detail = payload.difference_reason
    if reasons:
        detail = f"{detail}（不一致项：{'；'.join(reasons)}）" if detail else "；".join(reasons)

    review.status = (
        models.TrialOperationReview.STATUS_RETURNED
        if payload.return_to_report
        else models.TrialOperationReview.STATUS_REJECTED
    )
    review.settled_kwh = 0.0
    review.difference_kwh = round(review.review_kwh, 2)
    review.difference_reason = detail
    review.reviewer = payload.reviewer
    review.review_note = payload.review_note
    review.reviewed_at = date.today()
    return crud.save_review(db, review)


def reopen_review(
    db: Session, review: models.TrialOperationReview
) -> models.TrialOperationReview:
    """日报修正后重新进入待复核池。"""
    report = db.get(models.DailyReport, review.daily_report_id)
    if report is None:
        raise ValueError("关联日报不存在")
    review.status = models.TrialOperationReview.STATUS_PENDING
    review.review_kwh = report.grid_connected_kwh
    review.settled_kwh = 0.0
    review.difference_kwh = 0.0
    review.difference_reason = None
    review.reviewed_at = None
    return crud.save_review(db, review)


# ---------------- 并网批次对账单 ----------------

def curtailment_reason_label(reason_type: str) -> str:
    return _settlement_stats.curtailment_reason_label(reason_type)


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


def build_unit_reconciliation(
    db: Session,
    unit: models.Unit,
    reports: List[models.DailyReport],
) -> schemas.UnitReconciliationItem:
    accs = accumulate_grouped_by_unit(reports, db)
    acc = accs.get(unit.id)
    if acc is None:
        acc = UnitSettlementAccumulator(unit=unit)

    from app.settlement.stats import _build_unit_reconciliation_from_acc
    return _build_unit_reconciliation_from_acc(acc, reports)


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
    return _settlement_stats.build_reconciliation_report(
        db, start_date, end_date, batch
    )


def build_batch_reconciliation_report(
    db: Session,
    batch: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> schemas.BatchReconciliationItem:
    return _settlement_stats.build_batch_reconciliation_report(
        db, batch, start_date, end_date
    )


def build_unit_reconciliation_report(
    db: Session,
    unit_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> schemas.UnitReconciliationItem:
    return _settlement_stats.build_unit_reconciliation_report(
        db, unit_id, start_date, end_date
    )
