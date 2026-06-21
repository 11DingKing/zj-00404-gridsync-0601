"""初始数据：首批 4 台机组、若干天试运行/正常日报，以及 1 条升压站限发记录。"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal, create_tables, init_db

BATCH = "首批"
CAP_KW = 2000.0


def _report(d: str, gen: float, curtailed: float, fault: float, trial: bool) -> dict:
    return dict(
        report_date=date.fromisoformat(d),
        generation_kwh=gen,
        grid_connected_kwh=round(gen - curtailed, 1),
        curtailed_kwh=curtailed,
        fault_downtime_hours=fault,
        available_hours=round(24.0 - fault, 2),
        is_trial_operation=trial,
    )


def _do_seed(db: Session) -> None:
    GA = models.GridAcceptance

    # ---- 机组 ----
    units_data = [
        dict(code="WTG-01", name="1号风机", batch=BATCH, rated_capacity_kw=CAP_KW,
             commissioning_date=date(2026, 5, 20), location="A区"),
        dict(code="WTG-02", name="2号风机", batch=BATCH, rated_capacity_kw=CAP_KW,
             commissioning_date=date(2026, 5, 25), location="A区"),
        dict(code="WTG-03", name="3号风机", batch=BATCH, rated_capacity_kw=CAP_KW,
             commissioning_date=date(2026, 6, 12), location="B区"),
        dict(code="WTG-04", name="4号风机", batch=BATCH, rated_capacity_kw=CAP_KW,
             commissioning_date=None, location="B区"),
        dict(code="WTG-05", name="5号风机", batch="后续批次", rated_capacity_kw=CAP_KW,
             commissioning_date=date(2026, 6, 10), location="A区"),
        dict(code="WTG-06", name="6号风机", batch="后续批次", rated_capacity_kw=CAP_KW,
             commissioning_date=None, location="B区"),
    ]
    units: dict[str, models.Unit] = {}
    for u in units_data:
        unit = models.Unit(**u)
        db.add(unit)
        units[u["code"]] = unit
    db.flush()

    # ---- 并网验收 ----
    acc_data = [
        dict(unit_id=units["WTG-01"].id,
             application_status=GA.APP_APPROVED, application_date=date(2026, 6, 1),
             dispatch_permission_no="调度许字[2026]001号", dispatch_permission_date=date(2026, 6, 3),
             protection_setting_verified=True, protection_setting_verified_date=date(2026, 6, 4),
             trial_operation_hours=120.0, trial_operation_start_date=date(2026, 6, 5),
             trial_operation_end_date=date(2026, 6, 9),
             acceptance_result=GA.RESULT_PASSED, acceptance_date=date(2026, 6, 10),
             remark="首批首台，验收通过"),
        dict(unit_id=units["WTG-02"].id,
             application_status=GA.APP_APPROVED, application_date=date(2026, 6, 4),
             dispatch_permission_no="调度许字[2026]002号", dispatch_permission_date=date(2026, 6, 6),
             protection_setting_verified=True, protection_setting_verified_date=date(2026, 6, 7),
             trial_operation_hours=120.0, trial_operation_start_date=date(2026, 6, 8),
             trial_operation_end_date=date(2026, 6, 12),
             acceptance_result=GA.RESULT_PASSED, acceptance_date=date(2026, 6, 13),
             remark="验收通过"),
        dict(unit_id=units["WTG-03"].id,
             application_status=GA.APP_APPROVED, application_date=date(2026, 6, 10),
             dispatch_permission_no="调度许字[2026]003号", dispatch_permission_date=date(2026, 6, 12),
             protection_setting_verified=True, protection_setting_verified_date=date(2026, 6, 13),
             trial_operation_hours=96.0, trial_operation_start_date=date(2026, 6, 15),
             trial_operation_end_date=None,
             acceptance_result=GA.RESULT_TRIAL_OPERATION, acceptance_date=None,
             remark="试运行中，尚未验收"),
        dict(unit_id=units["WTG-04"].id,
             application_status=GA.APP_SUBMITTED, application_date=date(2026, 6, 18),
             dispatch_permission_no=None, dispatch_permission_date=None,
             protection_setting_verified=False, protection_setting_verified_date=None,
             trial_operation_hours=0.0, trial_operation_start_date=None,
             trial_operation_end_date=None,
             acceptance_result=GA.RESULT_PENDING, acceptance_date=None,
             remark="已提交并网申请，待调度许可"),
        dict(unit_id=units["WTG-05"].id,
             application_status=GA.APP_APPROVED, application_date=date(2026, 6, 12),
             dispatch_permission_no="调度许字[2026]004号", dispatch_permission_date=date(2026, 6, 14),
             protection_setting_verified=True, protection_setting_verified_date=date(2026, 6, 15),
             trial_operation_hours=72.0, trial_operation_start_date=date(2026, 6, 16),
             trial_operation_end_date=date(2026, 6, 18),
             acceptance_result=GA.RESULT_PASSED, acceptance_date=date(2026, 6, 19),
             remark="后续批次首台，验收通过"),
        dict(unit_id=units["WTG-06"].id,
             application_status=GA.APP_APPROVED, application_date=date(2026, 6, 15),
             dispatch_permission_no="调度许字[2026]005号", dispatch_permission_date=date(2026, 6, 17),
             protection_setting_verified=True, protection_setting_verified_date=date(2026, 6, 18),
             trial_operation_hours=48.0, trial_operation_start_date=date(2026, 6, 19),
             trial_operation_end_date=None,
             acceptance_result=GA.RESULT_TRIAL_OPERATION, acceptance_date=None,
             remark="后续批次，试运行中，尚未验收"),
    ]
    for a in acc_data:
        db.add(models.GridAcceptance(**a))
    db.flush()

    # ---- 日发电上报 ----
    reports_data: dict[str, list[dict]] = {
        "WTG-01": [
            _report("2026-06-05", 28500, 0, 0.0, True),
            _report("2026-06-06", 31200, 0, 0.0, True),
            _report("2026-06-07", 26800, 0, 1.5, True),
            _report("2026-06-18", 33400, 0, 0.0, False),
            _report("2026-06-19", 32100, 2000, 0.0, False),
            _report("2026-06-20", 30900, 0, 0.0, False),
        ],
        "WTG-02": [
            _report("2026-06-08", 29800, 0, 0.0, True),
            _report("2026-06-09", 27600, 0, 2.0, True),
            _report("2026-06-10", 31500, 0, 0.0, True),
            _report("2026-06-18", 32700, 0, 0.0, False),
            _report("2026-06-19", 31800, 2000, 0.0, False),
            _report("2026-06-20", 29500, 0, 0.0, False),
        ],
        "WTG-03": [
            _report("2026-06-18", 24300, 0, 0.0, True),
            _report("2026-06-19", 25600, 1500, 0.0, True),
            _report("2026-06-20", 23900, 0, 0.5, True),
        ],
        "WTG-05": [
            _report("2026-06-16", 22000, 0, 0.0, True),
            _report("2026-06-17", 24500, 0, 1.0, True),
            _report("2026-06-18", 23800, 0, 0.0, True),
            _report("2026-06-19", 26100, 0, 0.0, False),
            _report("2026-06-20", 25400, 2000, 0.0, False),
        ],
        "WTG-06": [
            _report("2026-06-19", 18000, 0, 0.0, True),
            _report("2026-06-20", 17200, 0, 2.0, True),
        ],
    }
    report_index: dict[tuple[str, date], models.DailyReport] = {}
    for code, rows in reports_data.items():
        uid = units[code].id
        for row in rows:
            rep = models.DailyReport(unit_id=uid, **row)
            db.add(rep)
            db.flush()
            report_index[(code, row["report_date"])] = rep

    # ---- 限发记录(1条) + 分摊 ----
    curtail_date = date(2026, 6, 19)
    cr = models.CurtailmentRecord(
        record_date=curtail_date,
        reason_type=models.CurtailmentRecord.REASON_BOOSTER_STATION,
        reason_detail="升压站主变容量受限，按调度指令对在运机组限发",
        total_curtailed_kwh=5500.0,
    )
    for code, kwh in [("WTG-01", 2000.0), ("WTG-02", 2000.0), ("WTG-03", 1500.0)]:
        rep = report_index[(code, curtail_date)]
        cr.allocations.append(
            models.CurtailmentAllocation(
                unit_id=units[code].id,
                daily_report_id=rep.id,
                allocated_curtailed_kwh=kwh,
            )
        )
    db.add(cr)

    # ---- 限发记录(6-20,送出线路) + 分摊(后续批次参与) ----
    curtail_date_2 = date(2026, 6, 20)
    cr2 = models.CurtailmentRecord(
        record_date=curtail_date_2,
        reason_type=models.CurtailmentRecord.REASON_TRANSMISSION_LINE,
        reason_detail="送出线路N-1校核受限，按调度指令对在运机组限发",
        total_curtailed_kwh=2000.0,
    )
    rep5_0620 = report_index[("WTG-05", curtail_date_2)]
    cr2.allocations.append(
        models.CurtailmentAllocation(
            unit_id=units["WTG-05"].id,
            daily_report_id=rep5_0620.id,
            allocated_curtailed_kwh=2000.0,
        )
    )
    db.add(cr2)

    # ---- 试运行扣减复核 ----
    TR = models.TrialOperationReview
    review_specs = [
        dict(code="WTG-01", d="2026-06-05", status=TR.STATUS_PASSED,
             settled=28500.0, diff_reason=None, reviewer="李调度",
             note="调度许可、验收结论、日报数据三者一致，复核通过"),
        dict(code="WTG-01", d="2026-06-06", status=TR.STATUS_PASSED,
             settled=30000.0, diff_reason="SCADA抄表核实，原上报31200，核实后30000，核减1200kWh",
             reviewer="李调度", note="上网电量存在差异，已扣减"),
        dict(code="WTG-01", d="2026-06-07", status=TR.STATUS_PENDING,
             settled=0.0, diff_reason=None, reviewer=None, note=None),
        dict(code="WTG-03", d="2026-06-18", status=TR.STATUS_RETURNED,
             settled=0.0, diff_reason="验收结论为试运行中尚未通过，退回日报修正",
             reviewer="李调度", note="待验收通过后重新发起复核"),
        dict(code="WTG-05", d="2026-06-16", status=TR.STATUS_PASSED,
             settled=22000.0, diff_reason=None, reviewer="李调度",
             note="调度许可、验收结论、日报数据三者一致，复核通过"),
        dict(code="WTG-05", d="2026-06-17", status=TR.STATUS_PASSED,
             settled=24000.0, diff_reason="SCADA抄表核实，原上报24500，核实后24000，核减500kWh",
             reviewer="李调度", note="上网电量存在差异，已扣减"),
        dict(code="WTG-05", d="2026-06-18", status=TR.STATUS_PASSED,
             settled=23800.0, diff_reason=None, reviewer="李调度",
             note="三者一致，复核通过"),
    ]
    review_count = 0
    for spec in review_specs:
        rep = report_index[(spec["code"], date.fromisoformat(spec["d"]))]
        uid = units[spec["code"]].id
        acc = db.query(models.GridAcceptance).filter_by(unit_id=uid).first()
        review_kwh = rep.grid_connected_kwh
        settled = spec["settled"]
        diff = round(review_kwh - settled, 2)
        rv = models.TrialOperationReview(
            daily_report_id=rep.id,
            unit_id=uid,
            review_date=rep.report_date,
            status=spec["status"],
            review_kwh=review_kwh,
            settled_kwh=settled,
            difference_kwh=diff,
            difference_reason=spec["diff_reason"],
            dispatch_permission_no=acc.dispatch_permission_no if acc else None,
            acceptance_result_snapshot=acc.acceptance_result if acc else None,
            reviewer=spec["reviewer"],
            review_note=spec["note"],
            reviewed_at=date(2026, 6, 20) if spec["status"] != TR.STATUS_PENDING else None,
        )
        db.add(rv)
        review_count += 1

    db.commit()
    unit_total = len(units_data)
    acc_total = len(acc_data)
    report_total = sum(len(v) for v in reports_data.values())
    print(
        f"已写入初始数据：{unit_total} 台机组（首批 4 + 后续批次 2）/ "
        f"{acc_total} 条验收记录 / {report_total} 条日报 / 2 条限发(4 条分摊) / "
        f"{review_count} 条试运行复核"
    )


def seed_if_empty() -> bool:
    create_tables()
    db = SessionLocal()
    try:
        if db.query(models.Unit).first() is not None:
            return False
        _do_seed(db)
        return True
    finally:
        db.close()


def reset_and_seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        _do_seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    reset_and_seed()
