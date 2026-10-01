import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.restaurant import Restaurant
from app.models.restaurant_document import RestaurantDocument
from app.models.restaurant_hours import RestaurantHours
from app.models.user import User, UserRole
from app.routers.admin_auth import current_admin


router = APIRouter(prefix="/api/admin/restaurants", tags=["admin restaurants"])


@router.get("", response_model=list[dict])
def list_restaurants(
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    records = (
        db.query(Restaurant, User)
        .join(User, User.id == Restaurant.manager_id)
        .filter(User.role == UserRole.MANAGER)
        .order_by(Restaurant.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "cuisine_type": restaurant.cuisine_type,
            "location": ", ".join(
                part for part in (restaurant.city, restaurant.state) if part
            ),
            "status": restaurant.status.value,
            "manager": {
                "id": str(manager.id),
                "name": manager.name,
                "email": manager.email,
            },
            "created_at": restaurant.created_at,
        }
        for restaurant, manager in records
    ]


@router.get("/{restaurant_id}", response_model=dict)
def get_restaurant_details(
    restaurant_id: uuid.UUID,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    record = (
        db.query(Restaurant, User)
        .join(User, User.id == Restaurant.manager_id)
        .filter(Restaurant.id == restaurant_id, User.role == UserRole.MANAGER)
        .first()
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found.",
        )

    restaurant, manager = record
    documents = (
        db.query(RestaurantDocument)
        .filter(RestaurantDocument.restaurant_id == restaurant.id)
        .order_by(RestaurantDocument.created_at.asc())
        .all()
    )
    hours = (
        db.query(RestaurantHours)
        .filter(RestaurantHours.restaurant_id == restaurant.id)
        .all()
    )
    weekday_order = {
        day: index
        for index, day in enumerate(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
    }
    hours.sort(key=lambda hour: weekday_order.get(hour.day_of_week.lower(), len(weekday_order)))

    return {
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
        "updated_at": restaurant.updated_at,
        "manager": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "status": manager.status.value,
            "is_active": manager.is_active,
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
                "day": hour.day_of_week,
                "enabled": hour.enabled,
                "opens": hour.open_time.strftime("%H:%M") if hour.open_time else None,
                "closes": hour.close_time.strftime("%H:%M") if hour.close_time else None,
            }
            for hour in hours
        ],
    }
