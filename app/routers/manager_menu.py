import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.database import get_db
from app.models.menu import Category, Food, FoodImage, FoodVariant
from app.core.cloudinary import delete_file, upload_file
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/manager/menu", tags=["manager menu"])
MAX_MENU_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_MENU_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class CategoryPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    display_order: int = Field(default=0, ge=0)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Category name is required.")
        if any(not (character.isalnum() or character.isspace() or character in "'-.") for character in value):
            raise ValueError("Category name may only contain letters, numbers, spaces, apostrophes, hyphens, and periods.")
        return value


class FoodPayload(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    is_available: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Food name is required.")
        if any(not (character.isalnum() or character.isspace() or character in "'-.") for character in value):
            raise ValueError("Food name may only contain letters, numbers, spaces, apostrophes, hyphens, and periods.")
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
        "variants": [{"id": str(item.id), "name": item.name, "price": float(item.price), "is_available": item.is_available} for item in food.variants],
        "images": [{"id": str(item.id), "url": item.image_url, "display_order": item.display_order} for item in food.images],
    }


class VariantPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(ge=0, le=99999999.99)
    is_available: bool = True

    @field_validator("name")
    @classmethod
    def clean_variant_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Variant name is required.")
        return value


def owned_food(food_id: uuid.UUID, restaurant_id: uuid.UUID, db: Session) -> Food:
    food = db.query(Food).filter(Food.id == food_id, Food.restaurant_id == restaurant_id, Food.deleted_at.is_(None)).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    return food


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
        db.query(Category).options(selectinload(Category.foods).selectinload(Food.variants), selectinload(Category.foods).selectinload(Food.images))
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


@router.post("/food/{food_id}/variants", status_code=status.HTTP_201_CREATED)
def create_variant(food_id: uuid.UUID, payload: VariantPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    food = owned_food(food_id, restaurant.id, db)
    variant = FoodVariant(food_id=food.id, **payload.model_dump())
    db.add(variant)
    db.commit()
    db.refresh(variant)
    return {"id": str(variant.id), "name": variant.name, "price": float(variant.price), "is_available": variant.is_available}


@router.patch("/food/{food_id}/variants/{variant_id}")
def update_variant(food_id: uuid.UUID, variant_id: uuid.UUID, payload: VariantPayload, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    food = owned_food(food_id, restaurant.id, db)
    variant = db.query(FoodVariant).filter(FoodVariant.id == variant_id, FoodVariant.food_id == food.id).first()
    if variant is None:
        raise HTTPException(status_code=404, detail="Menu variant not found.")
    for key, value in payload.model_dump().items():
        setattr(variant, key, value)
    db.commit()
    return {"id": str(variant.id), "name": variant.name, "price": float(variant.price), "is_available": variant.is_available}


@router.delete("/food/{food_id}/variants/{variant_id}")
def delete_variant(food_id: uuid.UUID, variant_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    restaurant = owned_restaurant(manager, db)
    food = owned_food(food_id, restaurant.id, db)
    variant = db.query(FoodVariant).filter(FoodVariant.id == variant_id, FoodVariant.food_id == food.id).first()
    if variant is None:
        raise HTTPException(status_code=404, detail="Menu variant not found.")
    db.delete(variant)
    db.commit()
    return {"message": "Menu variant deleted."}


@router.post("/food/{food_id}/images", status_code=status.HTTP_201_CREATED)
async def upload_food_image(food_id: uuid.UUID, image: UploadFile = File(...), manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    restaurant = owned_restaurant(manager, db)
    food = owned_food(food_id, restaurant.id, db)
    if image.content_type not in ALLOWED_MENU_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Dish images must be JPEG, PNG, or WebP.")
    content = await image.read(MAX_MENU_IMAGE_SIZE + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Choose an image to upload.")
    if len(content) > MAX_MENU_IMAGE_SIZE:
        raise HTTPException(status_code=413, detail="Dish images must be 10 MB or smaller.")
    valid_signature = (
        (image.content_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff"))
        or (image.content_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (image.content_type == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    )
    if not valid_signature:
        raise HTTPException(status_code=400, detail="The uploaded file does not match its image type.")
    try:
        result = upload_file(content, folder=f"dinebook/restaurants/{restaurant.id}/menu/{food.id}", resource_type="image")
        public_id, secure_url = result.get("public_id"), result.get("secure_url")
        if not public_id or not secure_url:
            raise RuntimeError("Image provider did not return a public ID and URL.")
        item = FoodImage(food_item_id=food.id, image_url=secure_url, public_id=public_id, display_order=len(food.images))
        db.add(item)
        db.commit()
        db.refresh(item)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        if "public_id" in locals() and public_id:
            try:
                delete_file(public_id)
            except Exception:
                pass
        raise HTTPException(status_code=502, detail="Unable to store this dish image.") from exc
    return {"id": str(item.id), "url": item.image_url, "display_order": item.display_order}


@router.delete("/food/{food_id}/images/{image_id}")
def delete_food_image(food_id: uuid.UUID, image_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    restaurant = owned_restaurant(manager, db)
    food = owned_food(food_id, restaurant.id, db)
    image = db.query(FoodImage).filter(FoodImage.id == image_id, FoodImage.food_item_id == food.id).first()
    if image is None:
        raise HTTPException(status_code=404, detail="Dish image not found.")
    public_id = image.public_id
    db.delete(image)
    db.commit()
    if public_id:
        try:
            delete_file(public_id, resource_type="image")
        except Exception:
            pass
    return {"message": "Dish image deleted."}
