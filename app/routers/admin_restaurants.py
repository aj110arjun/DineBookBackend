import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from app.db.database import get_db
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import RestaurantDocument
from app.models.restaurant_hours import RestaurantHours
from app.models.menu import Category, Food
from app.models.user import User, UserRole
from app.core.email import send_branded_email
from app.routers.admin_auth import current_admin


router = APIRouter(prefix="/api/admin/restaurants", tags=["admin restaurants"])


class SuspensionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/{restaurant_id}/suspend", response_model=dict)
def suspend_restaurant(restaurant_id: uuid.UUID, payload: SuspensionRequest,
                       admin: User = Depends(current_admin), db: Session = Depends(get_db)) -> dict:
    record = db.query(Restaurant, User).join(User, User.id == Restaurant.manager_id).filter(
        Restaurant.id == restaurant_id, User.role == UserRole.MANAGER
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    restaurant, manager = record
    if restaurant.status not in (RestaurantStatus.APPROVED, RestaurantStatus.SUSPENDED):
        raise HTTPException(status_code=409, detail="Only approved restaurants can be suspended.")
    if restaurant.status != RestaurantStatus.SUSPENDED:
        restaurant.status = RestaurantStatus.SUSPENDED
        db.commit()
    reason = payload.reason.strip() or "Please contact DineBook support for more information."
    email_sent = False
    try:
        send_branded_email(to=manager.email, subject=f"{restaurant.name} has been suspended on DineBook",
            title="Restaurant temporarily suspended", intro=f"Hello {manager.name},",
            detail=f"{restaurant.name} is no longer visible to customers. Your manager and chef portal access is paused until the restaurant is resumed. Reason: {reason}",
            plain_text=f"{restaurant.name} has been suspended. Reason: {reason}",
            error_detail="Email delivery is unavailable.")
        email_sent = True
    except Exception:
        pass
    return {"id": str(restaurant.id), "status": restaurant.status.value, "email_sent": email_sent}


@router.post("/{restaurant_id}/resume", response_model=dict)
def resume_restaurant(restaurant_id: uuid.UUID, admin: User = Depends(current_admin),
                     db: Session = Depends(get_db)) -> dict:
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found.")
    if restaurant.status != RestaurantStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="Restaurant is not suspended.")
    restaurant.status = RestaurantStatus.APPROVED
    db.commit()
    return {"id": str(restaurant.id), "status": restaurant.status.value}


@router.get("", response_model=list[dict])
def list_restaurants(
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    records = (
        db.query(Restaurant, User)
        .join(User, User.id == Restaurant.manager_id)
        .filter(
            User.role == UserRole.MANAGER,
            Restaurant.status.in_((RestaurantStatus.APPROVED, RestaurantStatus.SUSPENDED)),
        )
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
    menu_categories = (
        db.query(Category).options(selectinload(Category.foods).selectinload(Food.variants), selectinload(Category.foods).selectinload(Food.images))
        .filter(Category.restaurant_id == restaurant.id, Category.deleted_at.is_(None))
        .order_by(Category.display_order, Category.name).all()
    )

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
        "menu": [
            {
                "id": str(category.id), "name": category.name, "is_active": category.is_active,
                "foods": [
                    {
                        "id": str(food.id), "name": food.name, "description": food.description,
                        "is_available": food.is_available,
                        "variants": [{"id": str(item.id), "name": item.name, "price": float(item.price), "is_available": item.is_available} for item in food.variants],
                        "images": [{"id": str(item.id), "url": item.image_url} for item in food.images],
                    }
                    for food in category.foods if food.deleted_at is None
                ],
            }
            for category in menu_categories
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
