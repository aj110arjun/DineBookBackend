from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.schemas.admin_profile import (
	AdminDetails,
	AdminProfileResponse,
	AdminProfileUpdate,
)
from app.services import customer_profile


router = APIRouter(
	prefix="/api/admin/profile",
	tags=["admin profile"]
)


@router.get("", response_model=AdminProfileResponse)
def get_profile(admin: User = Depends(current_admin)):
	return {"admin": admin}


@router.patch("", response_model=AdminDetails)
def patch_profile(payload: AdminProfileUpdate, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
	return customer_profile.update_profile(db, admin, payload)


@router.post("/avatar", response_model=AdminDetails)
def upload_avatar(image: UploadFile = File(...), admin: User = Depends(current_admin), db: Session = Depends(get_db)):
	return customer_profile.replace_avatar(db, admin, image, folder="admin-avatars")


@router.delete("/avatar", response_model=AdminDetails)
def delete_avatar(admin: User = Depends(current_admin), db: Session = Depends(get_db)):
	return customer_profile.remove_avatar(db, admin)

