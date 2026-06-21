from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app import models
from app.settlement.caliber import normalize_daily_report_fields
from app.settlement.curtailment import (
    sum_allocated_curtailed_for_report,
    get_authoritative_curtailed_kwh,
)
from app.settlement.deductions import (
    DeductionItem,
    calculate_deductions,
    calculate_settlement_kwh,
    unit_is_settled,
)


@dataclass
class DailySettlementResult:
    """单日报结算结果：包含所有结算相关的计算值。"""
    report: models.DailyReport
    unit: models.Unit

    available_hours: float = 0.0
    grid_connected_kwh: float = 0.0
    generation_kwh: float = 0.0
    fault_downtime_hours: float = 0.0

    report_curtailed_kwh: float = 0.0
    allocated_curtailed_kwh: float = 0.0
    curtailed_kwh: float = 0.0

    is_trial_operation: bool = False
    trial_operation_kwh: float = 0.0
    pending_review_kwh: float = 0.0
    reviewed_settled_kwh: float = 0.0
    review_difference_kwh: float = 0.0
    review_difference_notes: List[str] = field(default_factory=list)

    deductions: List[DeductionItem] = field(default_factory=list)
    deduction_kwh: float = 0.0
    settlement_kwh: float = 0.0


def calculate_daily_settlement(
    report: models.DailyReport,
    unit: models.Unit,
    db: Optional[Session] = None,
) -> DailySettlementResult:
    """
    计算单日报的完整结算结果。
    
    这是结算的核心函数，所有统计维度都共用这一套口径。
    """
    cap = unit.rated_capacity_kw or 0.0
    available, normalized_grid = normalize_daily_report_fields(report, cap)

    report_curt = getattr(report, "curtailed_kwh", 0.0) or 0.0
    allocated = 0.0

    if db is not None:
        allocated = sum_allocated_curtailed_for_report(db, report)
    elif hasattr(report, "curtailment_allocations"):
        allocated = round(
            sum(a.allocated_curtailed_kwh for a in report.curtailment_allocations), 2
        )

    curt_authoritative = max(report_curt, allocated)

    deductions = calculate_deductions(report, unit, normalized_grid)
    deduction_total = round(sum(d.kwh for d in deductions), 2)
    settlement = calculate_settlement_kwh(normalized_grid, deductions)

    result = DailySettlementResult(
        report=report,
        unit=unit,
        available_hours=available,
        grid_connected_kwh=round(normalized_grid, 2),
        generation_kwh=getattr(report, "generation_kwh", 0.0) or 0.0,
        fault_downtime_hours=getattr(report, "fault_downtime_hours", 0.0) or 0.0,
        report_curtailed_kwh=round(report_curt, 2),
        allocated_curtailed_kwh=round(allocated, 2),
        curtailed_kwh=round(curt_authoritative, 2),
        is_trial_operation=getattr(report, "is_trial_operation", False),
        deductions=deductions,
        deduction_kwh=deduction_total,
        settlement_kwh=settlement,
    )

    _fill_trial_fields(result, report, unit)

    return result


def _fill_trial_fields(
    result: DailySettlementResult,
    report: models.DailyReport,
    unit: models.Unit,
) -> None:
    """填充试运行相关字段。"""
    if not report.is_trial_operation:
        return

    result.trial_operation_kwh = result.grid_connected_kwh

    review = getattr(report, "trial_review", None)
    if review is None:
        result.pending_review_kwh = result.grid_connected_kwh
        return

    if review.status == models.TrialOperationReview.STATUS_PASSED:
        result.reviewed_settled_kwh = round(review.settled_kwh, 2)
        if review.difference_kwh:
            result.review_difference_kwh = round(review.difference_kwh, 2)
        if review.difference_reason:
            result.review_difference_notes.append(review.difference_reason)
    else:
        result.pending_review_kwh = result.grid_connected_kwh
        if review.difference_reason:
            result.review_difference_notes.append(review.difference_reason)
            if review.difference_kwh:
                result.review_difference_kwh += round(review.difference_kwh, 2)


@dataclass
class UnitSettlementAccumulator:
    """累加单个机组在统计区间内的结算结果。"""
    unit: models.Unit

    generation_kwh: float = 0.0
    grid_connected_kwh: float = 0.0
    curtailed_kwh: float = 0.0
    allocated_curtailed_kwh: float = 0.0
    fault_downtime_hours: float = 0.0
    available_hours: float = 0.0
    settlement_kwh: float = 0.0
    trial_operation_kwh: float = 0.0
    pending_review_kwh: float = 0.0
    reviewed_settled_kwh: float = 0.0
    review_difference_kwh: float = 0.0
    review_difference_notes: List[str] = field(default_factory=list)
    deduction_kwh: float = 0.0
    deductions: List[DeductionItem] = field(default_factory=list)

    daily_results: List[DailySettlementResult] = field(default_factory=list)

    def add(self, result: DailySettlementResult) -> None:
        """累加单日报结算结果。"""
        self.generation_kwh += result.generation_kwh
        self.grid_connected_kwh += result.grid_connected_kwh
        self.curtailed_kwh += result.curtailed_kwh
        self.allocated_curtailed_kwh += result.allocated_curtailed_kwh
        self.fault_downtime_hours += result.fault_downtime_hours
        self.available_hours += result.available_hours
        self.settlement_kwh += result.settlement_kwh
        self.trial_operation_kwh += result.trial_operation_kwh
        self.pending_review_kwh += result.pending_review_kwh
        self.reviewed_settled_kwh += result.reviewed_settled_kwh
        self.review_difference_kwh += result.review_difference_kwh
        self.deduction_kwh += result.deduction_kwh

        if result.review_difference_notes:
            self.review_difference_notes.extend(result.review_difference_notes)
        if result.deductions:
            self.deductions.extend(result.deductions)

        self.daily_results.append(result)


def accumulate_unit_settlement(
    reports: List[models.DailyReport],
    unit: models.Unit,
    db: Optional[Session] = None,
) -> UnitSettlementAccumulator:
    """对一组日报（同一机组）进行结算累加。"""
    acc = UnitSettlementAccumulator(unit=unit)
    for report in reports:
        result = calculate_daily_settlement(report, unit, db)
        acc.add(result)
    return acc


def _round_accumulator(acc: UnitSettlementAccumulator) -> None:
    """对累加器的浮点值进行四舍五入（通常在输出前调用）。"""
    acc.generation_kwh = round(acc.generation_kwh, 2)
    acc.grid_connected_kwh = round(acc.grid_connected_kwh, 2)
    acc.curtailed_kwh = round(acc.curtailed_kwh, 2)
    acc.allocated_curtailed_kwh = round(acc.allocated_curtailed_kwh, 2)
    acc.fault_downtime_hours = round(acc.fault_downtime_hours, 2)
    acc.available_hours = round(acc.available_hours, 2)
    acc.settlement_kwh = round(acc.settlement_kwh, 2)
    acc.trial_operation_kwh = round(acc.trial_operation_kwh, 2)
    acc.pending_review_kwh = round(acc.pending_review_kwh, 2)
    acc.reviewed_settled_kwh = round(acc.reviewed_settled_kwh, 2)
    acc.review_difference_kwh = round(acc.review_difference_kwh, 2)
    acc.deduction_kwh = round(acc.deduction_kwh, 2)


def accumulate_grouped_by_unit(
    reports: List[models.DailyReport],
    db: Optional[Session] = None,
) -> Dict[int, UnitSettlementAccumulator]:
    """按机组分组对日报进行结算累加。"""
    reports_by_unit: Dict[int, List[models.DailyReport]] = {}
    units_by_id: Dict[int, models.Unit] = {}

    for r in reports:
        reports_by_unit.setdefault(r.unit_id, []).append(r)
        if r.unit_id not in units_by_id and hasattr(r, "unit"):
            units_by_id[r.unit_id] = r.unit

    result: Dict[int, UnitSettlementAccumulator] = {}
    for unit_id, unit_reports in reports_by_unit.items():
        unit = units_by_id.get(unit_id)
        if unit is None:
            if not unit_reports:
                continue
            unit = unit_reports[0].unit if hasattr(unit_reports[0], "unit") else None
        if unit is None:
            continue
        acc = accumulate_unit_settlement(unit_reports, unit, db)
        _round_accumulator(acc)
        result[unit_id] = acc

    return result
