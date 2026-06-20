from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/units", tags=["机组"])


@router.get("", response_model=List[schemas.UnitWithAcceptanceOut])
def list_units(batch: Optional[str] = None, db: Session = Depends(get_db)):
    return crud.list_units(db, batch=batch)


@router.post("", response_model=schemas.UnitOut, status_code=status.HTTP_201_CREATED)
def create_unit(payload: schemas.UnitCreate, db: Session = Depends(get_db)):
    if crud.get_unit_by_code(db, payload.code):
        raise HTTPException(status_code=400, detail="机组编号已存在")
    return crud.create_unit(db, payload)


@router.get("/{unit_id}", response_model=schemas.UnitWithAcceptanceOut)
def get_unit(unit_id: int, db: Session = Depends(get_db)):
    unit = crud.get_unit(db, unit_id)
    if not unit:
        raise HTTPException(status_code=404, detail="机组不存在")
    return unit


@router.delete("/{unit_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_unit(unit_id: int, db: Session = Depends(get_db)):
    unit = crud.get_unit(db, unit_id)
    if not unit:
        raise HTTPException(status_code=404, detail="机组不存在")
    crud.delete_unit(db, unit)
