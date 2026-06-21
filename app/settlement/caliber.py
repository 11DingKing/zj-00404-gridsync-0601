from __future__ import annotations

from typing import List, Tuple

from app import models

HOURS_PER_DAY = 24.0


def normalize_daily_report_fields(
    report: models.DailyReport | object,
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
    report: models.DailyReport | object,
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
