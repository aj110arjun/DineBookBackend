"""Manager-owned listing edits; verification documents and statuses are immutable."""
import io
from datetime import datetime, timezone
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.core.cloudinary import upload_file
from app.core.config import settings
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import RestaurantDocument, RestaurantDocumentType
from app.models.restaurant_hours import RestaurantHours
from app.routers.restaurants.operating_hours import effective_periods
from app.schemas.manager_profile import RestaurantDetails
from app.services.customer_profile import cleanup_avatar, validated_avatar
from app.services.table_availability import RESTAURANT_TIMEZONE


def owned_restaurant(db, manager, *, lock=False):
    query = db.query(Restaurant).filter(Restaurant.manager_id == manager.id)
    if lock:
        query = query.populate_existing().with_for_update()
    row = query.first()
    if row is None:
        raise HTTPException(404, "No restaurant is linked to this manager account.")
    return row


def restaurant_details(db, restaurant, *, now=None):
    documents = db.query(RestaurantDocument.id, RestaurantDocument.document_type,
                         RestaurantDocument.file_name, RestaurantDocument.created_at).filter(
        RestaurantDocument.restaurant_id == restaurant.id,
        RestaurantDocument.document_type != RestaurantDocumentType.BRANDING_IMAGE,
    ).order_by(RestaurantDocument.created_at, RestaurantDocument.id).limit(50).all()
    primary = db.query(RestaurantDocument.file_path).filter(
        RestaurantDocument.restaurant_id == restaurant.id,
        RestaurantDocument.document_type == RestaurantDocumentType.BRANDING_IMAGE,
    ).order_by(RestaurantDocument.created_at, RestaurantDocument.id).first()
    branding = primary.file_path if primary else None
    # Branding is public imagery; private verification file URLs never leave this API.
    if branding and not branding.startswith("https://"):
        branding = None
    local = (now or datetime.now(timezone.utc)).astimezone(RESTAURANT_TIMEZONE)
    periods, exception = effective_periods(db, restaurant.id, local.date())
    configured = exception is not None or db.query(RestaurantHours.id).filter(
        RestaurantHours.restaurant_id == restaurant.id).first() is not None
    clock = local.strftime("%H:%M")
    is_open = (restaurant.status == RestaurantStatus.APPROVED and
               any(period["opens"] <= clock < period["closes"] for period in periods)) if configured else None
    data = {field: getattr(restaurant, field) for field in (
        "id", "name", "description", "cuisine_type", "email", "phone", "address", "city", "state", "pin_code",
        "latitude", "longitude", "status", "created_at", "updated_at")}
    return RestaurantDetails(**data, branding_image_url=branding,
        documents=documents,
        operation={"date": local.date(), "timezone": str(RESTAURANT_TIMEZONE), "is_open_now": is_open,
                   "schedule_configured": configured, "periods": periods})


def update_restaurant(db, manager, payload):
    row = owned_restaurant(db, manager, lock=True)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to save restaurant details. Please try again.") from exc
    return restaurant_details(db, row)


def replace_branding(db, manager, image):
    restaurant = owned_restaurant(db, manager)
    content = validated_avatar(image, label="Restaurant images")
    if not all((settings.cloudinary_cloud_name, settings.cloudinary_api_key, settings.cloudinary_api_secret)):
        raise HTTPException(503, "Restaurant image uploads are currently unavailable.")
    public_id = None
    try:
        result = upload_file(io.BytesIO(content), folder=f"dinebook/restaurants/{restaurant.id}/branding", resource_type="image")
        public_id, url = result.get("public_id"), result.get("secure_url")
        if (not isinstance(public_id, str) or not 1 <= len(public_id) <= 255 or
                not isinstance(url, str) or not url.startswith("https://") or len(url) > 500):
            raise ValueError("Incomplete image provider response")
        restaurant = owned_restaurant(db, manager, lock=True)
        primary = db.query(RestaurantDocument).filter(
            RestaurantDocument.restaurant_id == restaurant.id,
            RestaurantDocument.document_type == RestaurantDocumentType.BRANDING_IMAGE,
        ).order_by(RestaurantDocument.created_at, RestaurantDocument.id).with_for_update().first()
        filename = PurePosixPath((image.filename or "restaurant-image").replace("\\", "/")).name
        filename = "".join(character for character in filename if character.isprintable())[:255] or "restaurant-image"
        if primary is None:
            primary = RestaurantDocument(restaurant_id=restaurant.id, document_type=RestaurantDocumentType.BRANDING_IMAGE)
            db.add(primary)
        primary.file_name, primary.file_path = filename, url
        restaurant.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        cleanup_avatar(public_id)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, "Unable to save the restaurant image. Please try again.") from exc
    # Existing document rows contain no Cloudinary public IDs. Retain detached
    # assets instead of guessing an ID and deleting a potentially unrelated file.
    return restaurant_details(db, restaurant)
