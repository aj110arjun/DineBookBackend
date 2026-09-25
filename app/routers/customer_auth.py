from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.schemas.customer import CustomerRegisterRequest, CustomerResponse

router = APIRouter(prefix="/api/auth/customer", tags=["customer authentication"])


@router.post("/register", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def register_customer(payload: CustomerRegisterRequest, db: Session = Depends(get_db)) -> User:
    email = str(payload.email).strip().lower()
    try:
        existing_user = db.query(User.id).filter(User.email == email).first()
    except ProgrammingError as exc:
        db.rollback()
        # PostgreSQL SQLSTATE 42P01 means the users table is missing. Keep the
        # response actionable without exposing database connection details.
        if getattr(exc.orig, "sqlstate", None) == "42P01" or getattr(exc.orig, "pgcode", None) == "42P01":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Customer database schema is not initialized. From Backend/, run 'venv/bin/alembic upgrade head'.",
            ) from exc
        raise
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    customer = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.CUSTOMER,
        status=AccountStatus.ACTIVE,
        is_active=True,
    )
    db.add(customer)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    db.refresh(customer)
    return customer
