import uuid
import secrets
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email import send_branded_email
from app.core.security import create_access_token, decode_access_token, hash_password, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, EmailVerificationCode, User, UserRole
from app.schemas.customer import CustomerRegisterRequest, CustomerResponse

router = APIRouter(prefix="/api/auth/customer", tags=["customer authentication"])
session_router = APIRouter(tags=["customer sessions"])
ACCESS_COOKIE = "dinebook_access_token"
GOOGLE_STATE_COOKIE = "dinebook_google_oauth_state"
GOOGLE_REDIRECT_COOKIE = "dinebook_google_oauth_redirect"


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class CustomerEmailCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class CustomerEmailRequest(BaseModel):
    email: EmailStr


class CustomerPasswordResetRequest(CustomerEmailCodeRequest):
    new_password: str = Field(min_length=10, max_length=128)


def verification_code_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    """Compare DB timestamps safely whether the driver returns them aware or naive."""
    current_time = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at <= current_time.astimezone(timezone.utc)


def send_verification_email(email: str, code: str) -> None:
    send_branded_email(
        to=email, subject="Your DineBook confirmation code", title="Confirm your email",
        intro="Thanks for signing up for DineBook. Enter this code to confirm your email address.",
        detail="This confirmation code expires in 2 minutes. If you didn’t create a DineBook account, you can ignore this email.",
        highlight_label="Your confirmation code", highlight_value=code,
        plain_text=f"Your DineBook confirmation code is {code}. It expires in 2 minutes.",
        error_detail="We couldn’t send the confirmation email. Please try again.",
    )


def issue_verification_code(db: Session, email: str) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    if record is None:
        record = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=datetime.now(timezone.utc) + timedelta(minutes=2))
        db.add(record)
    else:
        record.code_hash = hash_password(code)
        record.expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    db.flush()
    send_verification_email(email, code)


def send_password_reset_email(email: str, code: str) -> None:
    send_branded_email(
        to=email, subject="Your DineBook password reset code", title="Reset your password",
        intro="We received a request to reset the password for your DineBook account.",
        detail="This reset code expires in 2 minutes. If you didn’t request a password reset, ignore this email and your password will remain unchanged.",
        highlight_label="Your reset code", highlight_value=code,
        plain_text=f"Your DineBook password reset code is {code}. It expires in 2 minutes.",
        error_detail="We couldn’t send the reset email. Please try again.",
    )


@router.post("/forgot-password")
def request_password_reset(payload: CustomerEmailRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    # Keep the response the same whether or not the address has an account.
    if customer is None:
        return {"message": "If an account exists, a password reset code has been sent."}
    code = f"{secrets.randbelow(1_000_000):06d}"
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    if verification is None:
        verification = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=expires_at)
        db.add(verification)
    else:
        verification.code_hash = hash_password(code)
        verification.expires_at = expires_at
    try:
        db.flush()
        send_password_reset_email(email, code)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"message": "If an account exists, a password reset code has been sent."}


@router.post("/reset-password")
def reset_customer_password(payload: CustomerPasswordResetRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    now = datetime.now(timezone.utc)
    if customer is None or verification is None or verification_code_expired(verification.expires_at, now):
        raise HTTPException(status_code=400, detail="That reset code has expired. Request a new code.")
    if not verify_secret(payload.code, verification.code_hash):
        raise HTTPException(status_code=400, detail="That reset code is incorrect.")
    customer.password_hash = hash_password(payload.new_password)
    db.delete(verification)
    db.commit()
    return {"message": "Your password has been reset. You can now sign in."}


def current_customer(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(status_code=401, detail="Please sign in to continue.")
    if not token:
        raise unauthorized
    try:
        customer_id = uuid.UUID(decode_access_token(token).get("sub"))
    except (ValueError, TypeError, AttributeError):
        raise unauthorized
    customer = db.query(User).filter(User.id == customer_id, User.role == UserRole.CUSTOMER).first()
    if customer is None or not customer.is_active or not customer.email_verified:
        raise unauthorized
    return customer


@router.post("/login", response_model=CustomerResponse)
def login_customer(payload: CustomerLoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None or not verify_secret(payload.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not customer.is_active:
        raise HTTPException(status_code=403, detail="Your account is not active.")
    if not customer.email_verified:
        raise HTTPException(status_code=403, detail="Confirm your email address before signing in.")
    token, _ = create_access_token(str(customer.id))
    response.set_cookie(ACCESS_COOKIE, token, max_age=settings.jwt_expire_minutes * 60, httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/")
    return customer


@router.get("/google/login")
def google_login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")
    allowed_origins = {settings.frontend_url.rstrip("/")}
    if settings.frontend_public_url:
        allowed_origins.add(settings.frontend_public_url.rstrip("/"))
    origin_header = request.headers.get("origin") or request.headers.get("referer") or ""
    parsed_origin = urllib.parse.urlsplit(origin_header)
    request_origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}".rstrip("/") if parsed_origin.scheme and parsed_origin.netloc else ""
    if request_origin not in allowed_origins:
        forwarded_host = request.headers.get("x-forwarded-host", "")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        forwarded_origin = f"{forwarded_proto}://{forwarded_host}".rstrip("/") if forwarded_host and forwarded_proto else ""
        request_origin = forwarded_origin
    frontend_origin = request_origin if request_origin in allowed_origins else settings.frontend_url.rstrip("/")
    redirect_uri = f"{frontend_origin}/api/auth/customer/google/callback"
    state = secrets.token_urlsafe(32)
    query = urllib.parse.urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })
    redirect = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}", status_code=302)
    # Attach the CSRF state cookie to the redirect response itself. FastAPI
    # discards cookies set on an injected Response when a different response
    # object (RedirectResponse) is returned.
    secure_cookie = frontend_origin.startswith("https://") or settings.auth_cookie_secure
    redirect.set_cookie(GOOGLE_STATE_COOKIE, state, max_age=600, httponly=True,
                        secure=secure_cookie, samesite="lax", path="/")
    redirect.set_cookie(GOOGLE_REDIRECT_COOKIE, redirect_uri, max_age=600, httponly=True,
                        secure=secure_cookie, samesite="lax", path="/")
    return redirect


@router.get("/google/callback")
def google_callback(code: str | None = None, state: str | None = None,
                    error: str | None = None,
                    oauth_state: str | None = Cookie(default=None, alias=GOOGLE_STATE_COOKIE),
                    oauth_redirect_uri: str | None = Cookie(default=None, alias=GOOGLE_REDIRECT_COOKIE),
                    db: Session = Depends(get_db)):
    allowed_origins = {settings.frontend_url.rstrip("/")}
    if settings.frontend_public_url:
        allowed_origins.add(settings.frontend_public_url.rstrip("/"))
    redirect_suffix = "/api/auth/customer/google/callback"
    redirect_origin = ""
    if oauth_redirect_uri and oauth_redirect_uri.endswith(redirect_suffix):
        candidate_origin = oauth_redirect_uri[:-len(redirect_suffix)].rstrip("/")
        if candidate_origin in allowed_origins:
            redirect_origin = candidate_origin
    if not redirect_origin:
        redirect_origin = settings.frontend_url.rstrip("/")
    redirect_uri = f"{redirect_origin}{redirect_suffix}"
    secure_cookie = redirect_origin.startswith("https://") or settings.auth_cookie_secure

    def popup_result(result_status: str, token: str | None = None):
        target_origin = json.dumps(redirect_origin)
        safe_status = json.dumps(result_status)
        content = f"""<!doctype html><html><head><meta charset="utf-8"><title>DineBook sign-in</title></head>
<body><p>Completing Google sign-in… You may close this window.</p><script>
if (window.opener) {{
  window.opener.postMessage({{ type: "dinebook-google-auth", status: {safe_status} }}, {target_origin});
  window.setTimeout(() => window.close(), 1000);
}}
</script></body></html>"""
        result = HTMLResponse(content=content)
        if token:
            result.set_cookie(ACCESS_COOKIE, token, max_age=settings.jwt_expire_minutes * 60,
                              httponly=True, secure=secure_cookie, samesite="lax", path="/")
        result.delete_cookie(GOOGLE_STATE_COOKIE, path="/", httponly=True,
                             secure=secure_cookie, samesite="lax")
        result.delete_cookie(GOOGLE_REDIRECT_COOKIE, path="/", httponly=True,
                             secure=secure_cookie, samesite="lax")
        return result

    if error or not code or not state or not oauth_state or not secrets.compare_digest(state, oauth_state):
        return popup_result("failed")
    if not settings.google_client_id or not settings.google_client_secret:
        return popup_result("unavailable")
    try:
        token_request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode({
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(token_request, timeout=10) as result:
            access_token = json.loads(result.read()).get("access_token")
        if not access_token:
            raise ValueError("Missing Google access token")
        profile_request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(profile_request, timeout=10) as result:
            profile = json.loads(result.read())
        email = str(profile.get("email", "")).strip().lower()
        if not email or not profile.get("verified_email"):
            raise ValueError("Google account email is not verified")
    except Exception:
        return popup_result("failed")

    customer = db.query(User).filter(User.email == email).first()
    if customer is not None:
        if customer.role != UserRole.CUSTOMER or not customer.is_active:
            return popup_result("unavailable")
        customer.email_verified = True
        if not customer.name and profile.get("name"):
            customer.name = str(profile["name"])[:120]
    else:
        customer = User(
            name=str(profile.get("name") or email.split("@")[0])[:120],
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role=UserRole.CUSTOMER,
            status=AccountStatus.ACTIVE,
            is_active=True,
            email_verified=True,
        )
        db.add(customer)
    try:
        db.commit()
        db.refresh(customer)
    except IntegrityError:
        db.rollback()
        customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
        if customer is None or not customer.is_active:
            return popup_result("unavailable")
    token, _ = create_access_token(str(customer.id))
    return popup_result("success", token)


@session_router.get("/api/customer/me", response_model=CustomerResponse)
def get_current_customer(customer: User = Depends(current_customer)) -> User:
    return customer


@session_router.post("/api/auth/logout")
def logout_customer(response: Response) -> dict[str, str]:
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, secure=settings.auth_cookie_secure, samesite="lax")
    return {"message": "Signed out successfully."}


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
        email_verified=False,
    )
    db.add(customer)
    try:
        issue_verification_code(db, email)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    db.refresh(customer)
    return customer


@router.post("/verify-email")
def verify_customer_email(payload: CustomerEmailCodeRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No customer account was found for this email.")
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    now = datetime.now(timezone.utc)
    if verification is None or verification_code_expired(verification.expires_at, now):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That confirmation code has expired. Request a new code.")
    if customer.email_verified:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This email address is already confirmed. Sign in to continue.")
    if not verify_secret(payload.code, verification.code_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That confirmation code is incorrect.")
    customer.email_verified = True
    db.delete(verification)
    db.commit()
    return {"message": "Email address confirmed."}


@router.post("/resend-verification")
def resend_customer_verification(payload: CustomerEmailRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No customer account was found for this email.")
    if customer.email_verified:
        return {"message": "Email address is already confirmed."}
    try:
        issue_verification_code(db, email)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"message": "A new confirmation code has been sent."}
