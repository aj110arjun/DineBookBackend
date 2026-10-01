import uuid
import secrets
from datetime import datetime, time, timedelta, timezone

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
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.cloudinary import delete_file, upload_file
from app.core.config import settings
from app.core.email import send_branded_email
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
from app.models.user import EmailVerificationCode


router = APIRouter(
    prefix="/api/auth/manager",
    tags=["manager authentication"],
)


class ManagerEmailRequest(BaseModel):
    email: EmailStr


class ManagerEmailCodeRequest(ManagerEmailRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


def send_manager_verification_email(email: str, code: str) -> None:
    send_branded_email(
        to=email, subject="Your DineBook confirmation code", title="Confirm your email",
        intro="Thanks for starting your restaurant manager registration. Enter this code to confirm your email address.",
        detail="This confirmation code expires in 2 minutes. If you didn’t start this registration, you can ignore this email.",
        highlight_label="Your confirmation code", highlight_value=code,
        plain_text=f"Your DineBook confirmation code is {code}. It expires in 2 minutes.",
        error_detail="We couldn’t send the confirmation email. Please try again.",
    )


def issue_manager_verification_code(db: Session, email: str) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    if record is None:
        record = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=expires_at)
        db.add(record)
    else:
        record.code_hash = hash_password(code)
        record.expires_at = expires_at
    db.flush()
    send_manager_verification_email(email, code)


@router.post("/send-verification")
def send_manager_verification(payload: ManagerEmailRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in or use a different email.")
    try:
        issue_manager_verification_code(db, email)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"message": "A confirmation code has been sent."}


@router.post("/verify-email")
def verify_manager_email(payload: ManagerEmailCodeRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    code = payload.code
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    now = datetime.now(timezone.utc)
    if verification is None or verification.expires_at.replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(status_code=400, detail="That confirmation code has expired. Request a new code.")
    if not verify_secret(code, verification.code_hash):
        raise HTTPException(status_code=400, detail="That confirmation code is incorrect.")
    verification.code_hash = hash_password("manager-email-verified")
    db.commit()
    return {"message": "Email address confirmed."}

ACCESS_COOKIE = "dinebook_access_token"

MAX_DOCUMENT_SIZE = 5 * 1024 * 1024
MAX_IMAGE_SIZE = 10 * 1024 * 1024

ALLOWED_DOCUMENT_TYPES = {
    "application/pdf",
}

ALLOWED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
}


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
async def register_manager(
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
    latitude: float = Form(...),
    longitude: float = Form(...),

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
    branding_images: UploadFile = File(...),
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

    if not -90 <= latitude <= 90:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Longitude must be between -180 and 180.")

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

    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    if (
        verification is None
        or verification.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
        or not verify_secret("manager-email-verified", verification.code_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verify your email address before submitting the application.",
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
    db.delete(verification)
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
        latitude=latitude,
        longitude=longitude,
        status=RestaurantStatus.PENDING,
    )

    db.add(restaurant)
    db.flush()

    # Keep track of uploaded Cloudinary files so they can be
    # deleted if the database transaction fails.
    uploaded_files: list[tuple[str, str]] = []

    try:
        documents = [
            (
                fssai_license,
                RestaurantDocumentType.FSSAI_LICENSE,
                "raw",
                MAX_DOCUMENT_SIZE,
                ALLOWED_DOCUMENT_TYPES,
            ),
            (
                business_registration,
                RestaurantDocumentType.BUSINESS_REGISTRATION,
                "raw",
                MAX_DOCUMENT_SIZE,
                ALLOWED_DOCUMENT_TYPES,
            ),
            (
                owner_identity,
                RestaurantDocumentType.OWNER_IDENTITY,
                "raw",
                MAX_DOCUMENT_SIZE,
                ALLOWED_DOCUMENT_TYPES,
            ),
        ]

        if gst_certificate is not None:
            documents.append(
                (
                    gst_certificate,
                    RestaurantDocumentType.GST_CERTIFICATE,
                    "raw",
                    MAX_DOCUMENT_SIZE,
                    ALLOWED_DOCUMENT_TYPES,
                )
            )

        if branding_images is not None:
            documents.append(
                (
                    branding_images,
                    RestaurantDocumentType.BRANDING_IMAGE,
                    "image",
                    MAX_DOCUMENT_SIZE,
                    ALLOWED_IMAGE_TYPES,
                )
            )

        # Upload required/optional documents to Cloudinary.
        for (
            upload,
            document_type,
            resource_type,
            max_size,
            allowed_types,
        ) in documents:
            if not upload.filename:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded file must have a filename.",
                )

            if upload.content_type not in allowed_types:
                allowed_types_label = (
                    "PNG or JPEG images"
                    if allowed_types == ALLOWED_IMAGE_TYPES
                    else "PDF files"
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Unsupported file type for '{upload.filename}'. "
                        f"Allowed type: {allowed_types_label}."
                    ),
                )

            file_content = await upload.read()

            if (
                allowed_types == ALLOWED_DOCUMENT_TYPES
                and b"%PDF-" not in file_content[:1024]
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File '{upload.filename}' must be a valid PDF document.",
                )

            if len(file_content) > max_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File '{upload.filename}' is too large.",
                )

            result = upload_file(
                file=file_content,
                folder=(
                    f"dinebook/restaurants/"
                    f"{restaurant.id}/documents"
                ),
                resource_type=resource_type,
            )

            secure_url = result.get("secure_url")
            public_id = result.get("public_id")

            if not secure_url or not public_id:
                raise RuntimeError(
                    f"Cloudinary upload failed for '{upload.filename}'."
                )

            uploaded_files.append(
                (public_id, resource_type)
            )

            db.add(
                RestaurantDocument(
                    restaurant_id=restaurant.id,
                    document_type=document_type,
                    file_name=upload.filename,
                    file_path=secure_url,
                )
            )

        # Upload interior media separately.
        if interior_media is not None:
            if not interior_media.filename:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Interior media must have a filename.",
                )

            if interior_media.content_type not in ALLOWED_IMAGE_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Interior media must be a PNG or JPEG image.",
                )

            file_content = await interior_media.read()

            if len(file_content) > MAX_IMAGE_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Interior media is too large.",
                )

            result = upload_file(
                file=file_content,
                folder=(
                    f"dinebook/restaurants/"
                    f"{restaurant.id}/interior"
                ),
                resource_type="image",
            )

            secure_url = result.get("secure_url")
            public_id = result.get("public_id")

            if not secure_url or not public_id:
                raise RuntimeError(
                    "Cloudinary interior image upload failed."
                )

            uploaded_files.append(
                (public_id, "image")
            )

            db.add(
                RestaurantDocument(
                    restaurant_id=restaurant.id,
                    document_type=RestaurantDocumentType.BRANDING_IMAGE,
                    file_name=interior_media.filename,
                    file_path=secure_url,
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

        for public_id, resource_type in uploaded_files:
            try:
                delete_file(
                    public_id=public_id,
                    resource_type=resource_type,
                )
            except Exception:
                pass

        raise

    except (ValueError, TypeError) as exc:
        db.rollback()

        for public_id, resource_type in uploaded_files:
            try:
                delete_file(
                    public_id=public_id,
                    resource_type=resource_type,
                )
            except Exception:
                pass

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid manager application data: {str(exc)}",
        ) from exc

    except Exception as exc:
        db.rollback()

        for public_id, resource_type in uploaded_files:
            try:
                delete_file(
                    public_id=public_id,
                    resource_type=resource_type,
                )
            except Exception:
                pass

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
