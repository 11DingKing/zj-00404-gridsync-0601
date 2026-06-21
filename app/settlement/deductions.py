from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app import models


class DeductionType:
    UNACCEPTED = "unaccepted"
    PENDING_REVIEW = "pending_review"
    REVIEW_DIFFERENCE = "review_difference"


@dataclass
class DeductionItem:
    """扣减项明细：试运行待复核、复核核减、未通过验收等。"""
    type: str
    label: str
    kwh: float = 0.0
    reason: Optional[str] = None
    daily_report_id: Optional[int] = None
    review_id: Optional[int] = None


def unit_is_settled(unit: models.Unit) -> bool:
    """判断机组是否已通过并网验收（可进入结算）。"""
    return (
        unit.acceptance is not None
        and unit.acceptance.acceptance_result == models.GridAcceptance.RESULT_PASSED
    )


def _get_review_status_label(review: Optional[models.TrialOperationReview]) -> str:
    labels = {
        models.TrialOperationReview.STATUS_PENDING: "待复核",
        models.TrialOperationReview.STATUS_PASSED: "复核通过",
        models.TrialOperationReview.STATUS_REJECTED: "已驳回",
        models.TrialOperationReview.STATUS_RETURNED: "退回日报修正",
    }
    if review is None:
        return "未发起复核"
    return labels.get(review.status, review.status)


def _get_acceptance_result_label(acceptance_result: str) -> str:
    labels = {
        models.GridAcceptance.RESULT_PENDING: "待验收",
        models.GridAcceptance.RESULT_TRIAL_OPERATION: "试运行中",
        models.GridAcceptance.RESULT_PASSED: "验收通过",
        models.GridAcceptance.RESULT_FAILED: "验收未通过",
    }
    return labels.get(acceptance_result, acceptance_result)


def calculate_deductions(
    report: models.DailyReport,
    unit: models.Unit,
    grid_connected_kwh: float,
) -> List[DeductionItem]:
    """
    计算单日报的所有扣减项。
    
    扣减规则：
    1. 未通过并网验收：全部上网电量不计入结算
    2. 试运行 + 待复核/未通过：该日上网电量暂不入结算
    3. 试运行 + 复核通过 + 有差异：差异部分不计入结算
    """
    deductions: List[DeductionItem] = []
    settled = unit_is_settled(unit)

    if not settled:
        if grid_connected_kwh > 0:
            acc_result = unit.acceptance.acceptance_result if unit.acceptance else models.GridAcceptance.RESULT_PENDING
            deductions.append(DeductionItem(
                type=DeductionType.UNACCEPTED,
                label="未通过并网验收，电量暂不计入结算",
                kwh=round(grid_connected_kwh, 2),
                reason=f"验收结论：{_get_acceptance_result_label(acc_result)}",
            ))
        return deductions

    if not report.is_trial_operation:
        return deductions

    review = getattr(report, "trial_review", None)

    if review is not None and review.status == models.TrialOperationReview.STATUS_PASSED:
        diff = round(review.review_kwh - review.settled_kwh, 2)
        if diff > 0:
            deductions.append(DeductionItem(
                type=DeductionType.REVIEW_DIFFERENCE,
                label="复核核减电量",
                kwh=diff,
                reason=review.difference_reason,
                daily_report_id=report.id,
                review_id=review.id,
            ))
    else:
        status_label = _get_review_status_label(review)
        deductions.append(DeductionItem(
            type=DeductionType.PENDING_REVIEW,
            label="试运行待复核电量未转入结算",
            kwh=round(grid_connected_kwh, 2),
            reason=f"复核状态：{status_label}",
            daily_report_id=report.id,
            review_id=review.id if review else None,
        ))

    return deductions


def calculate_settlement_kwh(
    grid_connected_kwh: float,
    deductions: List[DeductionItem],
) -> float:
    """根据扣减项计算最终结算电量。"""
    total_deduction = sum(d.kwh for d in deductions)
    return round(max(0.0, grid_connected_kwh - total_deduction), 2)
