from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.customer_auth import current_customer
from app.schemas.customer import CustomerProfileUpdate, CustomerResponse
from app.services import customer_profile


router = APIRouter(prefix="/api/customer/profile", tags=["customer profile"])


@router.get("", response_model=CustomerResponse)
def get_profile(customer: User = Depends(current_customer)) -> User:
    return customer


@router.patch("", response_model=CustomerResponse)
def patch_profile(payload: CustomerProfileUpdate, customer: User = Depends(current_customer),
                  db: Session = Depends(get_db)) -> User:
    return customer_profile.update_profile(db, customer, payload)


@router.post("/avatar", response_model=CustomerResponse)
def upload_avatar(image: UploadFile = File(...), customer: User = Depends(current_customer),
                  db: Session = Depends(get_db)) -> User:
    return customer_profile.replace_avatar(db, customer, image)


@router.delete("/avatar", response_model=CustomerResponse)
def delete_avatar(customer: User = Depends(current_customer), db: Session = Depends(get_db)) -> User:
    return customer_profile.remove_avatar(db, customer)
