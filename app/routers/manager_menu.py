import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.database import get_db
from app.models.menu import Category, Food, FoodImage, FoodVariant
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/manager/menu", tags=["manager menu"])


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


class VariantPayload(BaseModel):
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    is_available: bool = True


class ImagePayload(BaseModel):
    image_url: HttpUrl
    display_order: int = Field(default=0, ge=0)


class FoodPayload(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_vegetarian: bool = False
    preparation_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    is_available: bool = True
    images: list[ImagePayload] = Field(default_factory=list, max_length=10)
    variants: list[VariantPayload] = Field(min_length=1, max_length=20)

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
        result["foods"] = [food_dict(food) for food in category.foods]
    return result


def food_dict(food: Food) -> dict:
    return {
        "id": str(food.id), "restaurant_id": str(food.restaurant_id), "category_id": str(food.category_id),
        "name": food.name, "description": food.description, "is_vegetarian": food.is_vegetarian,
        "preparation_time_minutes": food.preparation_time_minutes, "is_available": food.is_available,
        "images": [{"id": str(image.id), "image_url": image.image_url, "display_order": image.display_order} for image in food.images],
        "variants": [{"id": str(variant.id), "name": variant.name, "price": str(variant.price), "is_available": variant.is_available} for variant in food.variants],
    }


def owned_category(category_id: uuid.UUID, restaurant_id: uuid.UUID, db: Session) -> Category:
    category = db.query(Category).filter(Category.id == category_id, Category.restaurant_id == restaurant_id).first()
    if category is None:
        raise HTTPException(status_code=404, detail="Menu category not found.")
    return category


def apply_food_payload(food: Food, payload: FoodPayload) -> None:
    for key in ("category_id", "name", "description", "is_vegetarian", "preparation_time_minutes", "is_available"):
        setattr(food, key, getattr(payload, key))
    food.images = [FoodImage(image_url=str(image.image_url), display_order=image.display_order) for image in payload.images]
    food.variants = [FoodVariant(name=variant.name.strip(), price=variant.price, is_available=variant.is_available) for variant in payload.variants]


@router.get("")
def list_menu(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = owned_restaurant(manager, db)
    categories = (
        db.query(Category).options(selectinload(Category.foods).selectinload(Food.images), selectinload(Category.foods).selectinload(Food.variants))
        .filter(Category.restaurant_id == restaurant.id).order_by(Category.display_order, Category.name).all()
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
    category = owned_category(category_id, restaurant.id, db)
    db.delete(category)
    db.commit()
    return {"message": "Category and its menu items deleted."}


@router.post("/food", status_code=status.HTTP_201_CREATED)
def create_food(payload: FoodPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    owned_category(payload.category_id, restaurant.id, db)
    food = Food(restaurant_id=restaurant.id, category_id=payload.category_id, name=payload.name)
    apply_food_payload(food, payload)
    db.add(food)
    db.commit()
    db.refresh(food)
    return food_dict(food)


@router.patch("/food/{food_id}")
def update_food(food_id: uuid.UUID, payload: FoodPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    owned_category(payload.category_id, restaurant.id, db)
    food = db.query(Food).options(selectinload(Food.images), selectinload(Food.variants)).filter(Food.id == food_id, Food.restaurant_id == restaurant.id).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    apply_food_payload(food, payload)
    db.commit()
    db.refresh(food)
    return food_dict(food)


@router.delete("/food/{food_id}")
def delete_food(food_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    restaurant = owned_restaurant(manager, db)
    food = db.query(Food).filter(Food.id == food_id, Food.restaurant_id == restaurant.id).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    db.delete(food)
    db.commit()
    return {"message": "Menu item deleted."}
