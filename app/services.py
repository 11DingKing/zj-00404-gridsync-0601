from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload, selectinload

from app import crud, models, schemas

DIMENSIONS = {"daily", "monthly", "batch"}


class _UnitAccum:
    """累加单个机组在某统计区间内的电量指标。"""

    def __init__(self, unit: models.Unit) -> None:
        self.unit = unit
        self.generation_kwh = 0.0
        self.grid_connected_kwh = 0.0
        self.curtailed_kwh = 0.0
        self.fault_downtime_hours = 0.0
        self.settlement_kwh = 0.0
        self.trial_operation_kwh = 0.0
        self.pending_review_kwh = 0.0
        self.reviewed_settled_kwh = 0.0
        self.review_difference_kwh = 0.0
        self.review_difference_notes: List[str] = []

    def add(self, report: models.DailyReport) -> None:
        self.generation_kwh += report.generation_kwh
        self.grid_connected_kwh += report.grid_connected_kwh
        self.curtailed_kwh += report.curtailed_kwh
        self.fault_downtime_hours += report.fault_downtime_hours
        review = getattr(report, "trial_review", None)
        if report.is_trial_operation:
            self.trial_operation_kwh += report.grid_connected_kwh
            if review is not None and review.status == models.TrialOperationReview.STATUS_PASSED:
                self.settlement_kwh += review.settled_kwh
                self.reviewed_settled_kwh += review.settled_kwh
                if review.difference_kwh:
                    self.review_difference_kwh += review.difference_kwh
                    if review.difference_reason:
                        self.review_difference_notes.append(review.difference_reason)
            else:
                self.pending_review_kwh += report.grid_connected_kwh
                if review is not None and review.difference_reason:
                    self.review_difference_notes.append(review.difference_reason)
                    self.review_difference_kwh += review.difference_kwh
        elif _unit_settled(self.unit):
            self.settlement_kwh += report.grid_connected_kwh


def _unit_settled(unit: models.Unit) -> bool:
    return (
        unit.acceptance is not None
        and unit.acceptance.acceptance_result == models.GridAcceptance.RESULT_PASSED
    )


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


def _build_unit_item(acc: _UnitAccum) -> schemas.UnitStatsItem:
    cap = acc.unit.rated_capacity_kw or 0.0
    equiv = acc.grid_connected_kwh / cap if cap else 0.0
    return schemas.UnitStatsItem(
        unit_id=acc.unit.id,
        unit_code=acc.unit.code,
        batch=acc.unit.batch,
        rated_capacity_kw=round(cap, 2),
        generation_kwh=round(acc.generation_kwh, 2),
        grid_connected_kwh=round(acc.grid_connected_kwh, 2),
        curtailed_kwh=round(acc.curtailed_kwh, 2),
        fault_downtime_hours=round(acc.fault_downtime_hours, 2),
        settlement_kwh=round(acc.settlement_kwh, 2),
        trial_operation_kwh=round(acc.trial_operation_kwh, 2),
        pending_review_kwh=round(acc.pending_review_kwh, 2),
        reviewed_settled_kwh=round(acc.reviewed_settled_kwh, 2),
        review_difference_kwh=round(acc.review_difference_kwh, 2),
        review_difference_notes=list(acc.review_difference_notes),
        equivalent_utilization_hours=round(equiv, 2),
    )


def _build_group_item(group_key: str, accs: Dict[int, _UnitAccum]) -> schemas.StatsGroupItem:
    units = [_build_unit_item(a) for a in accs.values()]
    units.sort(key=lambda u: u.unit_code)
    total_cap = sum(u.rated_capacity_kw for u in units)
    total_grid = sum(u.grid_connected_kwh for u in units)
    equiv = total_grid / total_cap if total_cap else 0.0
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
        fault_downtime_hours=round(sum(u.fault_downtime_hours for u in units), 2),
        settlement_kwh=round(sum(u.settlement_kwh for u in units), 2),
        trial_operation_kwh=round(sum(u.trial_operation_kwh for u in units), 2),
        pending_review_kwh=round(sum(u.pending_review_kwh for u in units), 2),
        reviewed_settled_kwh=round(sum(u.reviewed_settled_kwh for u in units), 2),
        review_difference_kwh=round(sum(u.review_difference_kwh for u in units), 2),
        review_difference_notes=notes,
        units=units,
    )


def _accumulate_all(
    reports: List[models.DailyReport],
) -> Dict[int, _UnitAccum]:
    accs: Dict[int, _UnitAccum] = {}
    for r in reports:
        accs.setdefault(r.unit_id, _UnitAccum(r.unit)).add(r)
    return accs


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

    grouped: Dict[str, Dict[int, _UnitAccum]] = {}
    for r in reports:
        gk = _group_key(dimension, r)
        grouped.setdefault(gk, {}).setdefault(r.unit_id, _UnitAccum(r.unit)).add(r)

    group_items = [_build_group_item(gk, accs) for gk, accs in grouped.items()]
    group_items.sort(key=lambda g: g.group_key)

    totals = _build_group_item("总计(全周期)", _accumulate_all(reports))

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
    accs = _accumulate_all(reports)

    rows: List[schemas.SettlementRow] = []
    for acc in accs.values():
        unit = acc.unit
        settled = _unit_settled(unit)
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
