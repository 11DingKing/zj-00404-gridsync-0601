from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload, selectinload

from app import crud, models, schemas

DIMENSIONS = {"daily", "monthly", "batch"}
HOURS_PER_DAY = 24.0


# ---------------- 日报数据校验与规范化 ----------------
def normalize_daily_report_fields(
    report: models.DailyReport | schemas.DailyReportCreate | schemas.DailyReportUpdate,
    unit_capacity_kw: float,
) -> Tuple[float, float]:
    """
    规范化日报关键字段，保证口径一致：
      1. available_hours = 24 - fault_downtime_hours（限电不影响可用性）
      2. grid_connected_kwh = generation_kwh - curtailed_kwh
    返回 (normalized_available_hours, normalized_grid_connected_kwh)
    """
    fault = getattr(report, "fault_downtime_hours", 0.0) or 0.0
    gen = getattr(report, "generation_kwh", 0.0) or 0.0
    curt = getattr(report, "curtailed_kwh", 0.0) or 0.0

    fault = max(0.0, min(fault, HOURS_PER_DAY))
    available = round(HOURS_PER_DAY - fault, 4)
    grid = round(max(0.0, gen - curt), 2)
    return available, grid


def validate_daily_report_capacity(
    report: models.DailyReport | schemas.DailyReportCreate,
    unit_capacity_kw: float,
) -> List[str]:
    """
    校验日报数据合理性：
      - 发电量不得超过「额定容量 × 可用小时」的理论上限
      - 限电量不得超过「额定容量 × 可用小时 − 实际发电量」的剩余空间
    """
    errors: List[str] = []
    if unit_capacity_kw <= 0:
        return errors

    available, _ = normalize_daily_report_fields(report, unit_capacity_kw)
    gen = getattr(report, "generation_kwh", 0.0) or 0.0
    curt = getattr(report, "curtailed_kwh", 0.0) or 0.0

    theoretical_max = round(unit_capacity_kw * available, 2)
    if gen > theoretical_max + 0.01:
        errors.append(
            f"发电量 {gen} kWh 超过理论上限 {theoretical_max} kWh"
            f"（{unit_capacity_kw} kW × {available} 小时）"
        )

    remaining_capacity = max(0.0, theoretical_max - gen)
    if curt > remaining_capacity + 0.01:
        errors.append(
            f"限电量 {curt} kWh 超过剩余可用容量 {remaining_capacity} kWh"
            f"（理论上限 {theoretical_max} − 实际发电 {gen}）"
        )

    return errors


# ---------------- 限发分摊校验与计算 ----------------
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


def validate_curtailment_allocations(
    db: Session,
    record_date: date,
    allocations: List[schemas.CurtailmentAllocationCreate],
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
) -> List[schemas.CurtailmentAllocationCreate]:
    """
    按各机组当天「可用小时 × 额定容量」的权重来分摊总限电量。
    故障停机时段不计入分摊权重，避免把故障损失也算作限电损失。
    """
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


class _UnitAccum:
    """累加单个机组在某统计区间内的电量指标。"""

    def __init__(self, unit: models.Unit) -> None:
        self.unit = unit
        self.generation_kwh = 0.0
        self.grid_connected_kwh = 0.0
        self.curtailed_kwh = 0.0
        self.allocated_curtailed_kwh = 0.0
        self.fault_downtime_hours = 0.0
        self.available_hours = 0.0
        self.settlement_kwh = 0.0
        self.trial_operation_kwh = 0.0
        self.pending_review_kwh = 0.0
        self.reviewed_settled_kwh = 0.0
        self.review_difference_kwh = 0.0
        self.review_difference_notes: List[str] = []

    def add(self, report: models.DailyReport, db: Optional[Session] = None) -> None:
        available, normalized_grid = normalize_daily_report_fields(
            report, self.unit.rated_capacity_kw
        )

        self.generation_kwh += report.generation_kwh
        self.grid_connected_kwh += normalized_grid
        self.fault_downtime_hours += report.fault_downtime_hours
        self.available_hours += available

        allocated = 0.0
        if db is not None:
            allocated = sum_allocated_curtailed_for_report(db, report)
        elif hasattr(report, "curtailment_allocations"):
            allocated = round(
                sum(a.allocated_curtailed_kwh for a in report.curtailment_allocations), 2
            )
        self.allocated_curtailed_kwh += allocated
        self.curtailed_kwh += max(report.curtailed_kwh, allocated)

        review = getattr(report, "trial_review", None)
        if report.is_trial_operation:
            self.trial_operation_kwh += normalized_grid
            if review is not None and review.status == models.TrialOperationReview.STATUS_PASSED:
                self.settlement_kwh += review.settled_kwh
                self.reviewed_settled_kwh += review.settled_kwh
                if review.difference_kwh:
                    self.review_difference_kwh += review.difference_kwh
                    if review.difference_reason:
                        self.review_difference_notes.append(review.difference_reason)
            else:
                self.pending_review_kwh += normalized_grid
                if review is not None and review.difference_reason:
                    self.review_difference_notes.append(review.difference_reason)
                    self.review_difference_kwh += review.difference_kwh
        elif _unit_settled(self.unit):
            self.settlement_kwh += normalized_grid


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
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension 仅支持 {DIMENSIONS}")

    reports = _load_reports(db, start_date, end_date, batch, unit_id)
    grouped = _accumulate_grouped(reports, dimension, db)

    group_items: List[schemas.StatsGroupItem] = []
    for gk, accs in grouped.items():
        group_reports = [r for r in reports if _group_key(dimension, r) == gk]
        total_hours = _calc_total_hours(group_reports, dimension, gk)
        group_items.append(_build_group_item(gk, accs, total_hours))
    group_items.sort(key=lambda g: g.group_key)

    total_hours_all = _calc_total_hours(reports, dimension, None)
    totals = _build_group_item("总计(全周期)", _accumulate_all(reports, db), total_hours_all)

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
    accs = _accumulate_all(reports, db)

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


# ---------------- 并网批次对账单 ----------------
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
    cap = unit.rated_capacity_kw or 0.0
    acc = unit.acceptance
    settled = _unit_settled(unit)

    acceptance_result = acc.acceptance_result if acc else models.GridAcceptance.RESULT_PENDING
    trial_hours = (acc.trial_operation_hours or 0.0) if acc else 0.0
    dispatch_no = acc.dispatch_permission_no if acc else None

    grid_total = 0.0
    curtailed_total = 0.0
    allocated_total = 0.0
    settlement_total = 0.0
    deduction_items: List[schemas.DeductionItem] = []
    daily_refs: List[schemas.DailyReportRef] = []
    alloc_refs: List[schemas.CurtailmentAllocationRef] = []
    review_refs: List[schemas.TrialReviewRef] = []

    for report in reports:
        _, normalized_grid = normalize_daily_report_fields(report, cap)
        grid_total += normalized_grid
        curtailed_total += report.curtailed_kwh

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
            allocated_total += a.allocated_curtailed_kwh
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
        if report.is_trial_operation:
            if review is not None:
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
            if not settled:
                continue
            if review is not None and review.status == models.TrialOperationReview.STATUS_PASSED:
                settlement_total += review.settled_kwh
                diff = round(review.review_kwh - review.settled_kwh, 2)
                if diff > 0:
                    deduction_items.append(
                        schemas.DeductionItem(
                            type="review_difference",
                            label="复核核减电量",
                            kwh=diff,
                            reason=review.difference_reason,
                            daily_report_id=report.id,
                            review_id=review.id,
                        )
                    )
            else:
                status_label = _REVIEW_STATUS_LABELS.get(
                    review.status if review else None, "未发起复核"
                )
                deduction_items.append(
                    schemas.DeductionItem(
                        type="pending_review",
                        label="试运行待复核电量未转入结算",
                        kwh=round(normalized_grid, 2),
                        reason=f"复核状态：{status_label}",
                        daily_report_id=report.id,
                        review_id=review.id if review else None,
                    )
                )
        elif settled:
            settlement_total += normalized_grid

    if not settled:
        if grid_total > 0:
            deduction_items.insert(
                0,
                schemas.DeductionItem(
                    type="unaccepted",
                    label="未通过并网验收，电量暂不计入结算",
                    kwh=round(grid_total, 2),
                    reason=f"验收结论：{_ACCEPTANCE_RESULT_LABELS.get(acceptance_result, acceptance_result)}",
                ),
            )
        settlement_total = 0.0

    curtailed_authoritative = max(curtailed_total, allocated_total)

    return schemas.UnitReconciliationItem(
        unit_id=unit.id,
        unit_code=unit.code,
        unit_name=unit.name,
        batch=unit.batch,
        rated_capacity_kw=round(cap, 2),
        acceptance_status=_acceptance_status_summary(acc),
        acceptance_result=acceptance_result,
        dispatch_permission_no=dispatch_no,
        trial_operation_hours=round(trial_hours, 2),
        grid_connected_kwh=round(grid_total, 2),
        curtailed_kwh=round(curtailed_authoritative, 2),
        allocated_curtailed_kwh=round(allocated_total, 2),
        deduction_kwh=round(sum(d.kwh for d in deduction_items), 2),
        deduction_items=deduction_items,
        settlement_kwh=round(settlement_total, 2),
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
        unit_items = [
            build_unit_reconciliation(db, u, reports_by_unit.get(u.id, []))
            for u in batch_units
        ]
        batch_items.append(_summarize_batch(b, unit_items))
    batch_items.sort(key=lambda x: x.batch)

    all_unit_items = [
        build_unit_reconciliation(db, u, reports_by_unit.get(u.id, [])) for u in units
    ]
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
    unit_items = [
        build_unit_reconciliation(db, u, reports_by_unit.get(u.id, [])) for u in units
    ]
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
    return build_unit_reconciliation(db, unit, reports)
