import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import RestaurantDocument, RestaurantDocumentType
from app.models.restaurant_hours import RestaurantHours
from app.models.menu import Category, Food


router = APIRouter(prefix="/api/customer/restaurants", tags=["customer restaurants"])


@router.get("/{restaurant_id}/menu", response_model=list[dict])
def get_customer_restaurant_menu(restaurant_id: uuid.UUID, db: Session = Depends(get_db)) -> list[dict]:
    restaurant = db.query(Restaurant.id).filter(
        Restaurant.id == restaurant_id,
        Restaurant.status == RestaurantStatus.APPROVED,
    ).first()
    if restaurant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found.")

    categories = (
        db.query(Category)
        .filter(
            Category.restaurant_id == restaurant_id,
            Category.is_active.is_(True),
            Category.deleted_at.is_(None),
        )
        .order_by(Category.display_order, Category.name)
        .all()
    )
    return [
        {
            "id": str(category.id),
            "name": category.name,
            "description": category.description,
            "foods": [
                {
                    "id": str(food.id),
                    "name": food.name,
                    "description": food.description,
                    "is_available": food.is_available,
                }
                for food in sorted(category.foods, key=lambda food: food.name.lower())
                if food.deleted_at is None
            ],
        }
        for category in categories
    ]


@router.get("", response_model=list[dict])
def list_customer_restaurants(db: Session = Depends(get_db)) -> list[dict]:
    restaurants = (
        db.query(Restaurant)
        .filter(Restaurant.status == RestaurantStatus.APPROVED)
        .order_by(Restaurant.created_at.desc())
        .all()
    )
    images = (
        db.query(RestaurantDocument.restaurant_id, RestaurantDocument.file_path)
        .filter(
            RestaurantDocument.restaurant_id.in_([restaurant.id for restaurant in restaurants]),
            RestaurantDocument.document_type == RestaurantDocumentType.BRANDING_IMAGE,
        )
        .order_by(RestaurantDocument.created_at.asc())
        .all()
    )
    image_by_restaurant = {}
    for restaurant_id, image in images:
        image_by_restaurant.setdefault(restaurant_id, image)
    return [
        {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "description": restaurant.description,
            "cuisine": restaurant.cuisine_type or "Restaurant",
            "location": ", ".join(
                part for part in (restaurant.city, restaurant.state) if part
            ),
            "address": restaurant.address,
            "status": restaurant.status.value,
            "image": image_by_restaurant.get(restaurant.id),
            "created_at": restaurant.created_at,
        }
        for restaurant in restaurants
    ]


@router.get("/{restaurant_id}", response_model=dict)
def get_customer_restaurant(restaurant_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    restaurant = (
        db.query(Restaurant)
        .filter(
            Restaurant.id == restaurant_id,
            Restaurant.status == RestaurantStatus.APPROVED,
        )
        .first()
    )
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found.",
        )

    images = (
        db.query(RestaurantDocument.file_path)
        .filter(
            RestaurantDocument.restaurant_id == restaurant.id,
            RestaurantDocument.document_type == RestaurantDocumentType.BRANDING_IMAGE,
        )
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
        "cuisine": restaurant.cuisine_type or "Restaurant",
        "email": restaurant.email,
        "phone": restaurant.phone,
        "address": restaurant.address,
        "city": restaurant.city,
        "state": restaurant.state,
        "pin_code": restaurant.pin_code,
        "capacity": restaurant.capacity,
        "tables": restaurant.tables,
        "image": images[0][0] if images else None,
        "images": [image[0] for image in images],
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
