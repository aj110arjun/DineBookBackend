import uuid
from datetime import time
from pathlib import Path

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import EmailStr
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_secret,
)
from app.db.database import get_db
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import (
    RestaurantDocument,
    RestaurantDocumentType,
)
from app.models.restaurant_hours import RestaurantHours
from app.models.user import AccountStatus, User, UserRole


router = APIRouter(
    prefix="/api/auth/manager",
    tags=["manager authentication"],
)

ACCESS_COOKIE = "dinebook_access_token"

MAX_DOCUMENT_SIZE = 5 * 1024 * 1024
MAX_IMAGE_SIZE = 10 * 1024 * 1024

ALLOWED_DOCUMENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
}

ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
}

UPLOAD_ROOT = Path("uploads") / "restaurants"


def save_upload_file(
    upload: UploadFile,
    destination: Path,
    max_size: int,
    allowed_types: set[str],
) -> str:
    if not upload.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a filename.",
        )

    if upload.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type for '{upload.filename}'. "
                "Allowed types are PDF, PNG, and JPEG."
            ),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    total_size = 0

    try:
        with destination.open("wb") as output:
            while True:
                chunk = upload.file.read(1024 * 1024)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > max_size:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File '{upload.filename}' is too large.",
                    )

                output.write(chunk)

    except Exception:
        if destination.exists():
            destination.unlink()

        raise

    return str(destination)


def current_manager(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Please sign in as a manager to continue.",
    )

    if not token:
        raise unauthorized

    try:
        subject = decode_access_token(token).get("sub")
        manager_id = uuid.UUID(subject)

        manager = (
            db.query(User)
            .filter(
                User.id == manager_id,
                User.role == UserRole.MANAGER,
            )
            .first()
        )

    except (ValueError, TypeError):
        raise unauthorized

    if (
        manager is None
        or manager.status != AccountStatus.ACTIVE
        or not manager.is_active
        or not manager.email_verified
    ):
        raise unauthorized

    return manager


@router.post(
    "/register",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
def register_manager(
    name: str = Form(...),
    email: EmailStr = Form(...),
    password: str = Form(...),

    restaurant_name: str = Form(...),
    restaurant_description: str = Form(""),
    cuisine_type: str = Form(...),
    restaurant_contact: str = Form(...),
    restaurant_email: EmailStr = Form(...),

    address: str = Form(...),
    city: str = Form(...),
    state: str = Form(...),
    pin_code: str = Form(...),

    capacity: int = Form(...),
    tables: int = Form(...),

    monday_enabled: bool = Form(False),
    monday_open: str = Form("17:00"),
    monday_close: str = Form("22:00"),

    tuesday_enabled: bool = Form(False),
    tuesday_open: str = Form("17:00"),
    tuesday_close: str = Form("22:00"),

    wednesday_enabled: bool = Form(False),
    wednesday_open: str = Form("17:00"),
    wednesday_close: str = Form("22:00"),

    thursday_enabled: bool = Form(False),
    thursday_open: str = Form("17:00"),
    thursday_close: str = Form("22:00"),

    friday_enabled: bool = Form(False),
    friday_open: str = Form("17:00"),
    friday_close: str = Form("23:30"),

    saturday_enabled: bool = Form(False),
    saturday_open: str = Form("17:00"),
    saturday_close: str = Form("23:30"),

    sunday_enabled: bool = Form(False),
    sunday_open: str = Form("16:00"),
    sunday_close: str = Form("21:00"),

    fssai_license: UploadFile = File(...),
    business_registration: UploadFile = File(...),
    gst_certificate: UploadFile | None = File(None),
    owner_identity: UploadFile = File(...),
    branding_images: UploadFile | None = File(None),
    interior_media: UploadFile | None = File(None),

    db: Session = Depends(get_db),
) -> dict:
    name = name.strip()
    email = str(email).strip().lower()

    restaurant_name = restaurant_name.strip()
    restaurant_description = restaurant_description.strip()
    cuisine_type = cuisine_type.strip()
    restaurant_contact = restaurant_contact.strip()
    restaurant_email = str(restaurant_email).strip().lower()

    address = address.strip()
    city = city.strip()
    state = state.strip()
    pin_code = pin_code.strip()

    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Manager name is required.",
        )

    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must contain at least 8 characters.",
        )

    if capacity <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Capacity must be greater than zero.",
        )

    if tables <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Number of tables must be greater than zero.",
        )

    try:
        existing_user = (
            db.query(User.id)
            .filter(User.email == email)
            .first()
        )
    except ProgrammingError as exc:
        db.rollback()

        if (
            getattr(exc.orig, "sqlstate", None) == "42P01"
            or getattr(exc.orig, "pgcode", None) == "42P01"
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Manager database schema is not initialized. "
                    "From Backend/, run 'venv/bin/alembic upgrade head'."
                ),
            ) from exc

        raise

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    manager = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role=UserRole.MANAGER,
        status=AccountStatus.PENDING,
        is_active=False,
        email_verified=True,
    )

    db.add(manager)
    db.flush()

    restaurant = Restaurant(
        manager_id=manager.id,
        name=restaurant_name,
        description=restaurant_description or None,
        cuisine_type=cuisine_type,
        email=restaurant_email,
        phone=restaurant_contact,
        address=address,
        city=city,
        state=state,
        pin_code=pin_code,
        capacity=capacity,
        tables=tables,
        status=RestaurantStatus.PENDING,
    )

    db.add(restaurant)
    db.flush()

    uploaded_files: list[Path] = []

    try:
        documents = [
            (
                fssai_license,
                RestaurantDocumentType.FSSAI_LICENSE,
            ),
            (
                business_registration,
                RestaurantDocumentType.BUSINESS_REGISTRATION,
            ),
            (
                owner_identity,
                RestaurantDocumentType.OWNER_IDENTITY,
            ),
        ]

        if gst_certificate is not None:
            documents.append(
                (
                    gst_certificate,
                    RestaurantDocumentType.GST_CERTIFICATE,
                )
            )

        if branding_images is not None:
            documents.append(
                (
                    branding_images,
                    RestaurantDocumentType.BRANDING_IMAGE,
                )
            )

        for upload, document_type in documents:
            extension = Path(upload.filename or "").suffix.lower()

            generated_name = (
                f"{uuid.uuid4()}{extension}"
            )

            file_path = (
                UPLOAD_ROOT
                / str(restaurant.id)
                / generated_name
            )

            save_upload_file(
                upload=upload,
                destination=file_path,
                max_size=MAX_DOCUMENT_SIZE,
                allowed_types=ALLOWED_DOCUMENT_TYPES,
            )

            uploaded_files.append(file_path)

            db.add(
                RestaurantDocument(
                    restaurant_id=restaurant.id,
                    document_type=document_type,
                    file_name=upload.filename or generated_name,
                    file_path=str(file_path),
                )
            )

        if interior_media is not None:
            extension = Path(
                interior_media.filename or ""
            ).suffix.lower()

            generated_name = (
                f"{uuid.uuid4()}{extension}"
            )

            file_path = (
                UPLOAD_ROOT
                / str(restaurant.id)
                / generated_name
            )

            save_upload_file(
                upload=interior_media,
                destination=file_path,
                max_size=MAX_IMAGE_SIZE,
                allowed_types=ALLOWED_IMAGE_TYPES,
            )

            uploaded_files.append(file_path)

            db.add(
                RestaurantDocument(
                    restaurant_id=restaurant.id,
                    document_type=RestaurantDocumentType.BRANDING_IMAGE,
                    file_name=interior_media.filename or generated_name,
                    file_path=str(file_path),
                )
            )

        days = [
            (
                "monday",
                monday_enabled,
                monday_open,
                monday_close,
            ),
            (
                "tuesday",
                tuesday_enabled,
                tuesday_open,
                tuesday_close,
            ),
            (
                "wednesday",
                wednesday_enabled,
                wednesday_open,
                wednesday_close,
            ),
            (
                "thursday",
                thursday_enabled,
                thursday_open,
                thursday_close,
            ),
            (
                "friday",
                friday_enabled,
                friday_open,
                friday_close,
            ),
            (
                "saturday",
                saturday_enabled,
                saturday_open,
                saturday_close,
            ),
            (
                "sunday",
                sunday_enabled,
                sunday_open,
                sunday_close,
            ),
        ]

        for day_name, enabled, open_value, close_value in days:
            open_time = (
                time.fromisoformat(open_value)
                if enabled
                else None
            )

            close_time = (
                time.fromisoformat(close_value)
                if enabled
                else None
            )

            db.add(
                RestaurantHours(
                    restaurant_id=restaurant.id,
                    day_of_week=day_name,
                    enabled=enabled,
                    open_time=open_time,
                    close_time=close_time,
                )
            )

        db.commit()

    except HTTPException:
        db.rollback()

        for file_path in uploaded_files:
            if file_path.exists():
                file_path.unlink()

        raise

    except (ValueError, TypeError) as exc:
        db.rollback()

        for file_path in uploaded_files:
            if file_path.exists():
                file_path.unlink()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid manager application data.",
        ) from exc

    except Exception as exc:
        db.rollback()

        for file_path in uploaded_files:
            if file_path.exists():
                file_path.unlink()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to submit manager registration.",
        ) from exc

    db.refresh(manager)
    db.refresh(restaurant)

    return {
        "message": (
            "Manager registration submitted successfully. "
            "Your account is waiting for admin approval."
        ),
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
        "restaurant": {
            "id": str(restaurant.id),
            "name": restaurant.name,
            "status": restaurant.status.value,
        },
    }


@router.post("/login", response_model=dict)
def login_manager(
    payload: dict,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))

    manager = (
        db.query(User)
        .filter(
            User.email == email,
            User.role == UserRole.MANAGER,
        )
        .first()
    )

    if manager is None or not verify_secret(
        password,
        manager.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if manager.status == AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your manager registration is still waiting "
                "for admin approval."
            ),
        )

    if manager.status == AccountStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your manager registration has been rejected "
                "by the admin."
            ),
        )

    if (
        manager.status != AccountStatus.ACTIVE
        or not manager.is_active
        or not manager.email_verified
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your manager account is not active.",
        )

    token, _ = create_access_token(str(manager.id))

    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )

    return {
        "message": "Manager signed in successfully.",
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
    }


@router.get("/me", response_model=dict)
def get_current_manager(
    manager: User = Depends(current_manager),
) -> dict:
    return {
        "id": str(manager.id),
        "name": manager.name,
        "email": manager.email,
        "role": manager.role.value,
        "status": manager.status.value,
    }


@router.post("/logout")
def logout_manager(response: Response) -> dict[str, str]:
    response.delete_cookie(
        key=ACCESS_COOKIE,
        path="/",
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )

    return {
        "message": "Manager signed out successfully.",
    }