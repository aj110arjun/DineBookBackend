import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import RestaurantDocument
from app.models.restaurant_hours import RestaurantHours
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
    requests = (
        db.query(User, Restaurant)
        .join(Restaurant, Restaurant.manager_id == User.id)
        .filter(User.role == UserRole.MANAGER)
        .order_by(Restaurant.created_at.asc())
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
            "restaurant": {
                "id": str(restaurant.id),
                "name": restaurant.name,
                "cuisine_type": restaurant.cuisine_type,
                "city": restaurant.city,
                "state": restaurant.state,
                "status": restaurant.status.value,
            },
        }
        for manager, restaurant in requests
    ]

@router.get("/requests/{manager_id}", response_model=dict)
def get_manager_request_details(
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
            detail="Manager request not found.",
        )

    restaurant = (
        db.query(Restaurant)
        .filter(Restaurant.manager_id == manager.id)
        .first()
    )

    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant application not found.",
        )

    documents = (
        db.query(RestaurantDocument)
        .filter(RestaurantDocument.restaurant_id == restaurant.id)
        .order_by(RestaurantDocument.created_at.asc())
        .all()
    )

    hours = (
        db.query(RestaurantHours)
        .filter(RestaurantHours.restaurant_id == restaurant.id)
        .order_by(RestaurantHours.day_of_week.asc())
        .all()
    )

    return {
        "id": str(manager.id),
        "name": manager.name,
        "email": manager.email,
        "role": manager.role.value,
        "status": manager.status.value,
        "is_active": manager.is_active,
        "email_verified": manager.email_verified,
        "created_at": manager.created_at,

        "restaurant": {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "description": restaurant.description,
            "cuisine_type": restaurant.cuisine_type,
            "phone": restaurant.phone,
            "email": restaurant.email,
            "address": restaurant.address,
            "city": restaurant.city,
            "state": restaurant.state,
            "pin_code": restaurant.pin_code,
            "capacity": restaurant.capacity,
            "tables": restaurant.tables,
            "status": restaurant.status.value,
            "created_at": restaurant.created_at,
        },

        "documents": [
            {
                "id": str(document.id),
                "document_type": document.document_type.value,
                "file_name": document.file_name,
                "file_path": document.file_path,
                "created_at": document.created_at,
            }
            for document in documents
        ],

        "hours": [
            {
                "id": str(hour.id),
                "day_of_week": hour.day_of_week,
                "enabled": hour.enabled,
                "open_time": (
                    hour.open_time.strftime("%H:%M")
                    if hour.open_time
                    else None
                ),
                "close_time": (
                    hour.close_time.strftime("%H:%M")
                    if hour.close_time
                    else None
                ),
            }
            for hour in hours
        ],
    }

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

    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant application not found.",
        )
    if restaurant.status != RestaurantStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Restaurant application is already {restaurant.status.value.lower()}.",
        )

    manager.status = AccountStatus.ACTIVE
    manager.is_active = True
    restaurant.status = RestaurantStatus.APPROVED

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
        "restaurant": {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "status": restaurant.status.value,
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

    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant application not found.",
        )
    if restaurant.status != RestaurantStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Restaurant application is already {restaurant.status.value.lower()}.",
        )

    manager.status = AccountStatus.REJECTED
    manager.is_active = False
    restaurant.status = RestaurantStatus.REJECTED

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
        "restaurant": {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "status": restaurant.status.value,
        },
    }
