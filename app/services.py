from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.orm import Session, joinedload, selectinload

from app import models, schemas

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

    def add(self, report: models.DailyReport) -> None:
        self.generation_kwh += report.generation_kwh
        self.grid_connected_kwh += report.grid_connected_kwh
        self.curtailed_kwh += report.curtailed_kwh
        self.fault_downtime_hours += report.fault_downtime_hours
        # 结算规则：仅并网验收通过的机组、且非试运行期间的上网电量进入结算
        if _unit_settled(self.unit) and not report.is_trial_operation:
            self.settlement_kwh += report.grid_connected_kwh
        # 试运行期间的电量单独标记
        if report.is_trial_operation:
            self.trial_operation_kwh += report.grid_connected_kwh


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
        equivalent_utilization_hours=round(equiv, 2),
    )


def _build_group_item(group_key: str, accs: Dict[int, _UnitAccum]) -> schemas.StatsGroupItem:
    units = [_build_unit_item(a) for a in accs.values()]
    units.sort(key=lambda u: u.unit_code)
    total_cap = sum(u.rated_capacity_kw for u in units)
    total_grid = sum(u.grid_connected_kwh for u in units)
    equiv = total_grid / total_cap if total_cap else 0.0
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
        equivalent_utilization_hours=round(equiv, 2),
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
        elif acc.trial_operation_kwh > 0:
            excluded = f"试运行电量 {acc.trial_operation_kwh:.1f} kWh 已单独标记，不计入结算"
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
        rows=rows,
    )
