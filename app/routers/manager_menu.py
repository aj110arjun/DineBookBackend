import uuid
import json
from datetime import datetime, timezone
import re
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.cloudinary import delete_file, upload_file
from app.db.database import get_db
from app.models.menu import Category, Food, FoodImage, FoodVariant
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


class VariantPayload(BaseModel):
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    is_available: bool = True


class FoodPayload(BaseModel):
    category_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_vegetarian: bool = False
    preparation_time_minutes: int | None = Field(default=None, ge=1, le=1440)
    is_available: bool = True
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
        result["foods"] = [food_dict(food) for food in category.foods if food.deleted_at is None]
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
    category = db.query(Category).filter(Category.id == category_id, Category.restaurant_id == restaurant_id, Category.deleted_at.is_(None)).first()
    if category is None:
        raise HTTPException(status_code=404, detail="Menu category not found.")
    return category


def cloudinary_public_id_from_url(image_url: str) -> str | None:
    parsed = urlparse(image_url)
    if parsed.hostname != "res.cloudinary.com":
        return None
    path_parts = parsed.path.strip("/").split("/")
    try:
        upload_index = path_parts.index("upload")
    except ValueError:
        return None
    asset_parts = path_parts[upload_index + 1:]
    if asset_parts and re.fullmatch(r"v\d+", asset_parts[0]):
        asset_parts = asset_parts[1:]
    if not asset_parts:
        return None
    asset_parts[-1] = asset_parts[-1].rsplit(".", 1)[0]
    return "/".join(asset_parts) or None


def apply_food_payload(food: Food, payload: FoodPayload) -> None:
    for key in ("category_id", "name", "description", "is_vegetarian", "preparation_time_minutes", "is_available"):
        setattr(food, key, getattr(payload, key))
    food.variants = [FoodVariant(name=variant.name.strip(), price=variant.price, is_available=variant.is_available) for variant in payload.variants]


async def build_food_images(image_urls: str, image_files: list[UploadFile], restaurant_id: uuid.UUID, allowed_urls: set[str] | None = None) -> list[FoodImage]:
    try:
        retained_urls = json.loads(image_urls or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Invalid existing image list.") from exc
    if not isinstance(retained_urls, list) or any(not isinstance(url, str) or not url.startswith("https://") for url in retained_urls):
        raise HTTPException(status_code=422, detail="Image URLs must be secure HTTPS URLs.")
    if any(url not in (allowed_urls or set()) for url in retained_urls):
        raise HTTPException(status_code=422, detail="Existing images must belong to this menu item.")
    if len(retained_urls) + len(image_files) > 10:
        raise HTTPException(status_code=422, detail="A dish can have at most 10 images.")

    images = [FoodImage(image_url=url, display_order=index) for index, url in enumerate(retained_urls)]
    uploaded_public_ids: list[str] = []
    try:
        for upload in image_files:
            if upload.content_type not in ALLOWED_MENU_IMAGE_TYPES:
                raise HTTPException(status_code=415, detail="Upload a JPG, PNG, or WebP image.")
            content = await upload.read(MAX_MENU_IMAGE_SIZE + 1)
            if len(content) > MAX_MENU_IMAGE_SIZE:
                raise HTTPException(status_code=413, detail="Each image must be 10 MB or smaller.")
            upload.file.seek(0)
            result = upload_file(upload.file, folder=f"dinebook/restaurants/{restaurant_id}/menu", resource_type="image")
            public_id = result.get("public_id")
            secure_url = result.get("secure_url")
            if not public_id or not secure_url:
                raise RuntimeError("Cloudinary did not return an image URL.")
            uploaded_public_ids.append(public_id)
            images.append(FoodImage(image_url=secure_url, display_order=len(images)))
    except HTTPException:
        for public_id in uploaded_public_ids:
            try:
                delete_file(public_id)
            except Exception:
                pass
        raise
    except Exception as exc:
        for public_id in uploaded_public_ids:
            try:
                delete_file(public_id)
            except Exception:
                pass
        raise HTTPException(status_code=502, detail="Image upload failed. Please try again.") from exc
    return images


@router.get("")
def list_menu(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    restaurant = owned_restaurant(manager, db)
    categories = (
        db.query(Category).options(selectinload(Category.foods).selectinload(Food.images), selectinload(Category.foods).selectinload(Food.variants))
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
async def create_food(
    payload: str = Form(...), image_urls: str = Form("[]"), image_files: list[UploadFile] = File(default=[]),
    manager: User = Depends(current_manager), db: Session = Depends(get_db),
) -> dict:
    restaurant = owned_restaurant(manager, db)
    try:
        data = FoodPayload.model_validate_json(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid dish details.") from exc
    owned_category(data.category_id, restaurant.id, db)
    images = await build_food_images(image_urls, image_files, restaurant.id)
    food = Food(restaurant_id=restaurant.id, category_id=data.category_id, name=data.name, images=images)
    apply_food_payload(food, data)
    try:
        db.add(food)
        db.commit()
        db.refresh(food)
    except Exception as exc:
        db.rollback()
        for image in images:
            public_id = cloudinary_public_id_from_url(image.image_url)
            if public_id:
                try:
                    delete_file(public_id)
                except Exception:
                    pass
        raise HTTPException(status_code=500, detail="Unable to save this menu item.") from exc
    return food_dict(food)


@router.patch("/food/{food_id}")
async def update_food(
    food_id: uuid.UUID, payload: str = Form(...), image_urls: str = Form("[]"), image_files: list[UploadFile] = File(default=[]),
    manager: User = Depends(current_manager), db: Session = Depends(get_db),
) -> dict:
    restaurant = owned_restaurant(manager, db)
    try:
        data = FoodPayload.model_validate_json(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid dish details.") from exc
    owned_category(data.category_id, restaurant.id, db)
    food = db.query(Food).options(selectinload(Food.images), selectinload(Food.variants)).filter(Food.id == food_id, Food.restaurant_id == restaurant.id, Food.deleted_at.is_(None)).first()
    if food is None:
        raise HTTPException(status_code=404, detail="Menu item not found.")
    old_images_by_url = {image.image_url: cloudinary_public_id_from_url(image.image_url) for image in food.images}
    images = await build_food_images(image_urls, image_files, restaurant.id, set(old_images_by_url))
    retained_urls = {image.image_url for image in images}
    old_public_ids = [public_id for url, public_id in old_images_by_url.items() if public_id and url not in retained_urls]
    apply_food_payload(food, data)
    food.images = images
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        for image in images:
            public_id = cloudinary_public_id_from_url(image.image_url)
            if public_id:
                try:
                    delete_file(public_id)
                except Exception:
                    pass
        raise HTTPException(status_code=500, detail="Unable to save this menu item.") from exc
    db.refresh(food)
    for public_id in old_public_ids:
        try:
            delete_file(public_id)
        except Exception:
            pass
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
