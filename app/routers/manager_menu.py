import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.database import get_db
from app.models.menu import Category, Food
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/manager/menu", tags=["manager menu"])
MAX_MENU_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_MENU_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class CategoryPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    display_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Category name is required.")
        return value


class FoodPayload(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_available: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Food name is required.")
        return value


def owned_restaurant(manager: User, db: Session) -> Restaurant:
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    if restaurant is None:
        raise HTTPException(status_code=404, detail="No restaurant is linked to this manager account.")
    return restaurant


def category_dict(category: Category, include_food: bool = False) -> dict:
    result = {
        "id": str(category.id), "restaurant_id": str(category.restaurant_id), "name": category.name,
        "description": category.description, "display_order": category.display_order,
        "is_active": category.is_active,
    }
    if include_food:
        result["foods"] = [food_dict(food) for food in category.foods if food.deleted_at is None]
    return result


def food_dict(food: Food) -> dict:
    return {
        "id": str(food.id), "restaurant_id": str(food.restaurant_id), "category_id": str(food.category_id),
        "name": food.name, "description": food.description, "is_available": food.is_available,
    }


def owned_category(category_id: uuid.UUID, restaurant_id: uuid.UUID, db: Session) -> Category:
    category = db.query(Category).filter(Category.id == category_id, Category.restaurant_id == restaurant_id, Category.deleted_at.is_(None)).first()
    if category is None:
        raise HTTPException(status_code=404, detail="Menu category not found.")
    return category


def apply_food_payload(food: Food, payload: FoodPayload) -> None:
    for key in ("category_id", "name", "description", "is_available"):
        setattr(food, key, getattr(payload, key))


@router.get("")
def list_menu(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = owned_restaurant(manager, db)
    categories = (
        db.query(Category).options(selectinload(Category.foods))
        .filter(Category.restaurant_id == restaurant.id, Category.deleted_at.is_(None)).order_by(Category.display_order, Category.name).all()
    )
    return [category_dict(category, include_food=True) for category in categories]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    category = Category(restaurant_id=restaurant.id, **payload.model_dump())
    db.add(category)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A category with this name already exists.")
    db.refresh(category)
    return category_dict(category)


@router.patch("/categories/{category_id}")
def update_category(category_id: uuid.UUID, payload: CategoryPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    category = owned_category(category_id, restaurant.id, db)
    for key, value in payload.model_dump().items():
        setattr(category, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A category with this name already exists.")
    db.refresh(category)
    return category_dict(category)


@router.delete("/categories/{category_id}")
def delete_category(category_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    restaurant = owned_restaurant(manager, db)
    category = db.query(Category).options(selectinload(Category.foods)).filter(Category.id == category_id, Category.restaurant_id == restaurant.id, Category.deleted_at.is_(None)).first()
    if category is None:
        raise HTTPException(status_code=404, detail="Menu category not found.")
    deleted_at = datetime.now(timezone.utc)
    category.deleted_at = deleted_at
    for food in category.foods:
        if food.deleted_at is None:
            food.deleted_at = deleted_at
    db.commit()
    return {"message": "Category and its menu items were moved to deleted items."}


@router.post("/food", status_code=status.HTTP_201_CREATED)
def create_food(
    payload: str = Form(...),
    manager: User = Depends(current_manager), db: Session = Depends(get_db),
) -> dict:
    restaurant = owned_restaurant(manager, db)
    try:
        data = FoodPayload.model_validate_json(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid dish details.") from exc
    owned_category(data.category_id, restaurant.id, db)
    food = Food(restaurant_id=restaurant.id, category_id=data.category_id, name=data.name)
    apply_food_payload(food, data)
    try:
        db.add(food)
        db.commit()
        db.refresh(food)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Unable to save this menu item.") from exc
    return food_dict(food)


@router.patch("/food/{food_id}")
def update_food(
    food_id: uuid.UUID, payload: str = Form(...),
    manager: User = Depends(current_manager), db: Session = Depends(get_db),
) -> dict:
    restaurant = owned_restaurant(manager, db)
    try:
        data = FoodPayload.model_validate_json(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid dish details.") from exc
    owned_category(data.category_id, restaurant.id, db)
    food = db.query(Food).filter(Food.id == food_id, Food.restaurant_id == restaurant.id, Food.deleted_at.is_(None)).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    apply_food_payload(food, data)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Unable to save this menu item.") from exc
    db.refresh(food)
    return food_dict(food)


@router.delete("/food/{food_id}")
def delete_food(food_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    restaurant = owned_restaurant(manager, db)
    food = db.query(Food).filter(Food.id == food_id, Food.restaurant_id == restaurant.id).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    food.deleted_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Menu item moved to deleted items."}
