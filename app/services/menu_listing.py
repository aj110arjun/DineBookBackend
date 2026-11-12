"""Database-paginated menu browsing shared by customer and administrator views."""
import uuid

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app.models.menu import Category, Food, FoodVariant
from app.models.restaurant import Restaurant, RestaurantStatus


def menu_page(db, restaurant_id: uuid.UUID, *, page: int, page_size: int,
              search: str = "", category_id: uuid.UUID | None = None,
              availability: str = "ALL", sort: str = "name", public: bool = False,
              preorder: bool = False, is_vegetarian: bool | None = None):
    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None or (public and restaurant.status != RestaurantStatus.APPROVED):
        raise HTTPException(404, "Restaurant not found.")
    categories_query = db.query(Category).filter(Category.restaurant_id == restaurant_id, Category.deleted_at.is_(None))
    if public:
        categories_query = categories_query.filter(Category.is_active.is_(True))
    categories = categories_query.order_by(Category.display_order, Category.name, Category.id).all()
    if public and not preorder and category_id is None and categories:
        category_id = categories[0].id
    query = db.query(Food).join(Category).filter(Food.restaurant_id == restaurant_id, Food.deleted_at.is_(None), Category.deleted_at.is_(None))
    if public:
        query = query.filter(Category.is_active.is_(True), Food.is_available.is_(True))
    elif availability != "ALL":
        query = query.filter(Food.is_available.is_(availability == "AVAILABLE"))
    if preorder:
        query = query.filter(Food.variants.any(
            (FoodVariant.deleted_at.is_(None)) & (FoodVariant.is_available.is_(True))))
    if is_vegetarian is not None:
        query = query.filter(Food.is_vegetarian.is_(is_vegetarian))
    if search.strip():
        needle = f"%{search.strip()}%"
        matches = [Food.name.ilike(needle), Food.description.ilike(needle), Category.name.ilike(needle)]
        if preorder:
            matches.append(Food.variants.any(
                (FoodVariant.deleted_at.is_(None)) & (FoodVariant.is_available.is_(True))
                & FoodVariant.name.ilike(needle)))
        query = query.filter(or_(*matches))
    category_counts = dict(query.with_entities(Food.category_id, func.count(Food.id)).group_by(Food.category_id).all())
    if category_id:
        query = query.filter(Food.category_id == category_id)
    total = query.count()
    price = db.query(func.min(FoodVariant.price)).filter(FoodVariant.food_id == Food.id, FoodVariant.deleted_at.is_(None), FoodVariant.is_available.is_(True)).correlate(Food).scalar_subquery()
    ordering = {"name": func.lower(Food.name).asc(), "name-desc": func.lower(Food.name).desc(),
                "price-low": price.asc().nulls_last(), "price-high": price.desc().nulls_first()}[sort]
    foods = query.options(selectinload(Food.variants), selectinload(Food.image), selectinload(Food.category)).order_by(ordering, Food.id).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [{"id": str(food.id), "name": food.name, "description": food.description,
                   "category_id": str(food.category_id), "category_name": food.category.name,
                   "is_available": food.is_available, "is_vegetarian": food.is_vegetarian,
                   "restaurant_name": restaurant.name,
                   "image_url": food.image.image_url if food.image else None,
                   "variants": [{"id": str(v.id), "name": v.name, "price": float(v.price), "is_available": v.is_available}
                                for v in food.variants if v.deleted_at is None and (not preorder or v.is_available)]} for food in foods],
        "categories": [{"id": str(c.id), "name": c.name, "description": c.description,
                        "is_active": c.is_active, "count": category_counts.get(c.id, 0)} for c in categories],
        "total": total, "page": page, "page_size": page_size,
    }
