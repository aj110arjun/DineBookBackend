"""Customer-owned profile changes and managed image storage."""

import io
import logging
import warnings

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.cloudinary import delete_file, upload_file
from app.core.config import settings
from app.models.user import User
from app.schemas.customer import CustomerProfileUpdate


logger = logging.getLogger(__name__)
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_AVATAR_PIXELS = 20_000_000
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def validated_avatar(image: UploadFile, *, label: str = "Profile photos") -> bytes:
    expected_format = IMAGE_FORMATS.get(image.content_type)
    if expected_format is None:
        raise HTTPException(status_code=400, detail=f"{label} must be JPEG, PNG, or WebP.")
    content = image.file.read(MAX_AVATAR_BYTES + 1)
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} must be 5 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as picture:
                if picture.format != expected_format or picture.width * picture.height > MAX_AVATAR_PIXELS:
                    raise ValueError("Invalid image format or dimensions")
                picture.verify()
            # verify() alone does not decode JPEG/WebP pixels or detect every truncated image.
            with Image.open(io.BytesIO(content)) as picture:
                picture.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=400, detail="Choose a valid, undamaged JPEG, PNG, or WebP photo (up to 20 megapixels).") from exc
    return content


def cleanup_avatar(public_id: str | None) -> None:
    if not public_id:
        return
    try:
        result = delete_file(public_id, resource_type="image")
        if result.get("result") not in {"ok", "not found"}:
            logger.warning("Profile photo cleanup was not acknowledged by storage.")
    except Exception:
        # The committed profile remains usable if the provider is temporarily unavailable.
        logger.warning("Unable to clean up a detached profile photo.")


def update_profile(db: Session, customer: User, payload: CustomerProfileUpdate) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Unable to save your profile. Please try again.") from exc
    return customer


def replace_avatar(db: Session, customer: User, image: UploadFile, *, folder: str = "customer-avatars") -> User:
    content = validated_avatar(image)
    if not all((settings.cloudinary_cloud_name, settings.cloudinary_api_key, settings.cloudinary_api_secret)):
        raise HTTPException(status_code=503, detail="Profile photo uploads are currently unavailable.")
    new_public_id = None
    try:
        result = upload_file(io.BytesIO(content), folder=f"dinebook/{folder}/{customer.id}", resource_type="image")
        new_public_id, url = result.get("public_id"), result.get("secure_url")
        if (not isinstance(new_public_id, str) or not 1 <= len(new_public_id) <= 255
                or not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048):
            raise ValueError("Incomplete image provider response")
        # Refresh under a row lock so simultaneous uploads/deletes detach the actual previous image.
        customer = db.query(User).filter(User.id == customer.id).populate_existing().with_for_update().one()
        old_public_id = customer.avatar_public_id
        customer.avatar_url, customer.avatar_public_id = url, new_public_id
        db.commit()
    except Exception as exc:
        db.rollback()
        cleanup_avatar(new_public_id)
        raise HTTPException(status_code=502, detail="Unable to save your profile photo. Please try again.") from exc
    if old_public_id != new_public_id:
        cleanup_avatar(old_public_id)
    return customer


def remove_avatar(db: Session, customer: User) -> User:
    try:
        customer = db.query(User).filter(User.id == customer.id).populate_existing().with_for_update().one()
        old_public_id = customer.avatar_public_id
        customer.avatar_url, customer.avatar_public_id = None, None
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Unable to remove your profile photo. Please try again.") from exc
    cleanup_avatar(old_public_id)
    return customer
