import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.routers.admin_auth import current_admin


router = APIRouter(
    prefix="/api/admin/managers",
    tags=["admin manager management"],
)


@router.get("/requests", response_model=list[dict])
def get_manager_requests(
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    managers = (
        db.query(User)
        .filter(
            User.role == UserRole.MANAGER,
            User.status == AccountStatus.PENDING,
        )
        .order_by(User.created_at.asc())
        .all()
    )

    return [
        {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
            "created_at": manager.created_at,
        }
        for manager in managers
    ]


@router.patch("/{manager_id}/approve", response_model=dict)
def approve_manager(
    manager_id: uuid.UUID,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    manager = (
        db.query(User)
        .filter(
            User.id == manager_id,
            User.role == UserRole.MANAGER,
        )
        .first()
    )

    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manager account not found.",
        )

    if manager.status != AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Manager account is already {manager.status.value.lower()}.",
        )

    manager.status = AccountStatus.ACTIVE
    manager.is_active = True

    db.commit()
    db.refresh(manager)

    return {
        "message": "Manager approved successfully.",
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
    }


@router.patch("/{manager_id}/reject", response_model=dict)
def reject_manager(
    manager_id: uuid.UUID,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    manager = (
        db.query(User)
        .filter(
            User.id == manager_id,
            User.role == UserRole.MANAGER,
        )
        .first()
    )

    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manager account not found.",
        )

    if manager.status != AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Manager account is already {manager.status.value.lower()}.",
        )

    manager.status = AccountStatus.REJECTED
    manager.is_active = False

    db.commit()
    db.refresh(manager)

    return {
        "message": "Manager registration rejected.",
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
    }
