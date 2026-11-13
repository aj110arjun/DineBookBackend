from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.auth.manager_auth import current_manager
from app.schemas.manager_profile import ManagerDetails, ManagerProfileResponse, ManagerProfileUpdate, RestaurantDetails, RestaurantProfileUpdate
from app.services import customer_profile, manager_profile

router = APIRouter(prefix="/api/manager/profile", tags=["manager profile"])


@router.get("", response_model=ManagerProfileResponse)
def get_profile(manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    restaurant = db.query(Restaurant).filter(Restaurant.manager_id == manager.id).first()
    return {"manager": manager, "restaurant": manager_profile.restaurant_details(db, restaurant) if restaurant else None}


@router.patch("", response_model=ManagerDetails)
def patch_profile(payload: ManagerProfileUpdate, manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return customer_profile.update_profile(db, manager, payload)


@router.post("/avatar", response_model=ManagerDetails)
def upload_avatar(image: UploadFile = File(...), manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return customer_profile.replace_avatar(db, manager, image, folder="manager-avatars")


@router.delete("/avatar", response_model=ManagerDetails)
def delete_avatar(manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return customer_profile.remove_avatar(db, manager)


@router.patch("/restaurant", response_model=RestaurantDetails)
def patch_restaurant(payload: RestaurantProfileUpdate, manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return manager_profile.update_restaurant(db, manager, payload)


@router.post("/restaurant/branding-image", response_model=RestaurantDetails)
def upload_branding(image: UploadFile = File(...), manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return manager_profile.replace_branding(db, manager, image)
