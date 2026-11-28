from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.auth.chef_auth import current_chef
from app.schemas.chef_profile import ChefDetails, ChefProfileResponse, ChefProfileUpdate
from app.services import customer_profile

router = APIRouter(prefix="/api/chef/profile", tags=["chef profile"])


@router.get("", response_model=ChefProfileResponse)
def get_profile(chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    restaurant = None
    if chef.manager_id is not None:
        restaurant = db.query(Restaurant).filter(Restaurant.manager_id == chef.manager_id).first()
    return {"chef": chef, "restaurant": restaurant}


@router.patch("", response_model=ChefDetails)
def patch_profile(payload: ChefProfileUpdate, chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    return customer_profile.update_profile(db, chef, payload)


@router.post("/avatar", response_model=ChefDetails)
def upload_avatar(image: UploadFile = File(...), chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    return customer_profile.replace_avatar(db, chef, image, folder="chef-avatars")


@router.delete("/avatar", response_model=ChefDetails)
def delete_avatar(chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    return customer_profile.remove_avatar(db, chef)
