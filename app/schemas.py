from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Unit ----------
class UnitBase(BaseModel):
    code: str = Field(..., description="机组编号，如 WTG-01")
    name: str
    batch: str = Field(..., description="所属批次，如 首批")
    rated_capacity_kw: float = Field(..., gt=0, description="额定容量(kW)")
    commissioning_date: Optional[date] = None
    location: Optional[str] = None


class UnitCreate(UnitBase):
    pass


class UnitOut(UnitBase, ORMBase):
    id: int


# ---------- GridAcceptance ----------
class GridAcceptanceBase(BaseModel):
    application_status: str = "not_submitted"
    application_date: Optional[date] = None
    dispatch_permission_no: Optional[str] = None
    dispatch_permission_date: Optional[date] = None
    protection_setting_verified: bool = False
    protection_setting_verified_date: Optional[date] = None
    trial_operation_hours: float = 0.0
    trial_operation_start_date: Optional[date] = None
    trial_operation_end_date: Optional[date] = None
    acceptance_result: str = "pending"
    acceptance_date: Optional[date] = None
    remark: Optional[str] = None


class GridAcceptanceCreate(GridAcceptanceBase):
    unit_id: int


class GridAcceptanceUpdate(BaseModel):
    application_status: Optional[str] = None
    application_date: Optional[date] = None
    dispatch_permission_no: Optional[str] = None
    dispatch_permission_date: Optional[date] = None
    protection_setting_verified: Optional[bool] = None
    protection_setting_verified_date: Optional[date] = None
    trial_operation_hours: Optional[float] = None
    trial_operation_start_date: Optional[date] = None
    trial_operation_end_date: Optional[date] = None
    acceptance_result: Optional[str] = None
    acceptance_date: Optional[date] = None
    remark: Optional[str] = None


class GridAcceptanceOut(GridAcceptanceBase, ORMBase):
    id: int
    unit_id: int


class UnitWithAcceptanceOut(UnitOut):
    acceptance: Optional[GridAcceptanceOut] = None


# ---------- DailyReport ----------
class DailyReportBase(BaseModel):
    report_date: date
    generation_kwh: float = Field(0.0, ge=0)
    grid_connected_kwh: float = Field(0.0, ge=0)
    curtailed_kwh: float = Field(0.0, ge=0)
    fault_downtime_hours: float = Field(0.0, ge=0)
    available_hours: float = Field(0.0, ge=0)
    is_trial_operation: bool = False
    remark: Optional[str] = None


class DailyReportCreate(DailyReportBase):
    unit_id: int


class DailyReportUpdate(BaseModel):
    generation_kwh: Optional[float] = None
    grid_connected_kwh: Optional[float] = None
    curtailed_kwh: Optional[float] = None
    fault_downtime_hours: Optional[float] = None
    available_hours: Optional[float] = None
    is_trial_operation: Optional[bool] = None
    remark: Optional[str] = None


class DailyReportOut(DailyReportBase, ORMBase):
    id: int
    unit_id: int


# ---------- Curtailment ----------
class CurtailmentAllocationCreate(BaseModel):
    unit_id: int
    daily_report_id: Optional[int] = None
    allocated_curtailed_kwh: float = Field(..., ge=0)


class CurtailmentAllocationOut(ORMBase):
    id: int
    curtailment_record_id: int
    unit_id: int
    daily_report_id: Optional[int] = None
    allocated_curtailed_kwh: float


class CurtailmentRecordBase(BaseModel):
    record_date: date
    reason_type: str
    reason_detail: Optional[str] = None
    total_curtailed_kwh: float = Field(0.0, ge=0)


class CurtailmentRecordCreate(CurtailmentRecordBase):
    allocations: List[CurtailmentAllocationCreate] = Field(default_factory=list)


class CurtailmentRecordOut(CurtailmentRecordBase, ORMBase):
    id: int
    allocations: List[CurtailmentAllocationOut] = []


# ---------- Statistics ----------
class UnitStatsItem(BaseModel):
    unit_id: int
    unit_code: str
    batch: str
    rated_capacity_kw: float
    generation_kwh: float
    grid_connected_kwh: float
    curtailed_kwh: float
    fault_downtime_hours: float
    settlement_kwh: float
    trial_operation_kwh: float
    equivalent_utilization_hours: float


class StatsGroupItem(BaseModel):
    group_key: str
    unit_count: int
    rated_capacity_kw: float
    generation_kwh: float
    grid_connected_kwh: float
    curtailed_kwh: float
    fault_downtime_hours: float
    settlement_kwh: float
    trial_operation_kwh: float
    equivalent_utilization_hours: float
    units: List[UnitStatsItem]


class StatisticsResponse(BaseModel):
    dimension: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    batch: Optional[str] = None
    totals: StatsGroupItem
    groups: List[StatsGroupItem]


# ---------- Settlement ----------
class SettlementRow(BaseModel):
    unit_id: int
    unit_code: str
    batch: str
    rated_capacity_kw: float
    grid_connected_kwh: float
    settlement_kwh: float
    trial_operation_kwh: float
    excluded_reason: Optional[str] = None


class SettlementReport(BaseModel):
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_settlement_kwh: float
    total_grid_connected_kwh: float
    total_trial_operation_kwh: float
    rows: List[SettlementRow]
