from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Unit(Base):
    """风电机组：每台机组的静态台账信息。"""

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    batch: Mapped[str] = mapped_column(String(32), index=True)
    rated_capacity_kw: Mapped[float] = mapped_column(Float)
    commissioning_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    acceptance: Mapped[Optional["GridAcceptance"]] = relationship(
        back_populates="unit", uselist=False, cascade="all, delete-orphan"
    )
    daily_reports: Mapped[List["DailyReport"]] = relationship(
        back_populates="unit", cascade="all, delete-orphan"
    )


class GridAcceptance(Base):
    """并网验收：管理申请、调度许可、保护定值核对、试运行小时数与验收结论。"""

    __tablename__ = "grid_acceptances"

    APP_NOT_SUBMITTED = "not_submitted"
    APP_SUBMITTED = "submitted"
    APP_APPROVED = "approved"

    RESULT_PENDING = "pending"
    RESULT_TRIAL_OPERATION = "trial_operation"
    RESULT_PASSED = "passed"
    RESULT_FAILED = "failed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), unique=True, index=True)

    application_status: Mapped[str] = mapped_column(String(32), default=APP_NOT_SUBMITTED)
    application_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dispatch_permission_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dispatch_permission_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    protection_setting_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    protection_setting_verified_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    trial_operation_hours: Mapped[float] = mapped_column(Float, default=0.0)
    trial_operation_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    trial_operation_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    acceptance_result: Mapped[str] = mapped_column(String(32), default=RESULT_PENDING)
    acceptance_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="acceptance")


class DailyReport(Base):
    """日发电上报：每台机组每天上报发电量、限电量、故障停机时长和可利用小时。"""

    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    generation_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    grid_connected_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    curtailed_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    fault_downtime_hours: Mapped[float] = mapped_column(Float, default=0.0)
    available_hours: Mapped[float] = mapped_column(Float, default=0.0)
    is_trial_operation: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="daily_reports")
    curtailment_allocations: Mapped[List["CurtailmentAllocation"]] = relationship(
        back_populates="daily_report", cascade="all, delete-orphan"
    )


class CurtailmentRecord(Base):
    """限发记录：升压站/送出线路容量受限等原因及总限电量，并分摊到具体机组。"""

    __tablename__ = "curtailment_records"

    REASON_BOOSTER_STATION = "booster_station_capacity"
    REASON_TRANSMISSION_LINE = "transmission_line_capacity"
    REASON_GRID_DISPATCH = "grid_dispatch"
    REASON_EQUIPMENT_FAULT = "equipment_fault"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_date: Mapped[date] = mapped_column(Date, index=True)
    reason_type: Mapped[str] = mapped_column(String(32))
    reason_detail: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    total_curtailed_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    allocations: Mapped[List["CurtailmentAllocation"]] = relationship(
        back_populates="curtailment_record", cascade="all, delete-orphan"
    )


class CurtailmentAllocation(Base):
    """限发分摊：把某次限发记录的限电量分摊到具体机组的某天日报。"""

    __tablename__ = "curtailment_allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    curtailment_record_id: Mapped[int] = mapped_column(
        ForeignKey("curtailment_records.id"), index=True
    )
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), index=True)
    daily_report_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("daily_reports.id"), nullable=True
    )
    allocated_curtailed_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    curtailment_record: Mapped["CurtailmentRecord"] = relationship(back_populates="allocations")
    daily_report: Mapped[Optional["DailyReport"]] = relationship(
        back_populates="curtailment_allocations"
    )
