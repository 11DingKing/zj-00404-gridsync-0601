from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/acceptance", tags=["并网验收"])


@router.get("/by-unit/{unit_id}", response_model=schemas.GridAcceptanceOut)
def get_by_unit(unit_id: int, db: Session = Depends(get_db)):
    obj = crud.get_acceptance_by_unit(db, unit_id)
    if not obj:
        raise HTTPException(status_code=404, detail="该机组暂无验收记录")
    return obj


@router.post(
    "", response_model=schemas.GridAcceptanceOut, status_code=status.HTTP_201_CREATED
)
def create_acceptance(
    payload: schemas.GridAcceptanceCreate, db: Session = Depends(get_db)
):
    if not crud.get_unit(db, payload.unit_id):
        raise HTTPException(status_code=404, detail="机组不存在")
    if crud.get_acceptance_by_unit(db, payload.unit_id):
        raise HTTPException(status_code=400, detail="该机组已有验收记录")
    return crud.create_acceptance(db, payload)


@router.patch("/{acceptance_id}", response_model=schemas.GridAcceptanceOut)
def update_acceptance(
    acceptance_id: int,
    payload: schemas.GridAcceptanceUpdate,
    db: Session = Depends(get_db),
):
    obj = crud.get_acceptance(db, acceptance_id)
    if not obj:
        raise HTTPException(status_code=404, detail="验收记录不存在")
    return crud.update_acceptance(db, obj, payload)
