# DineBook Backend

FastAPI API for the DineBook customer, manager, chef, and platform administrator portals. It uses PostgreSQL, SQLAlchemy 2, Alembic migrations, signed JWT session cookies, SMTP email, Google OAuth, and Cloudinary for uploaded restaurant and menu images.

## Requirements

- Python 3.12 (the checked-in virtual environment is under `Backend/venv`)
- PostgreSQL
- SMTP credentials for email verification and portal email workflows
- Google OAuth credentials for customer Google sign-in
- Cloudinary credentials for restaurant documents/interior images and menu images

## Configure and run locally

Create a PostgreSQL role and database (or use an existing database):

```sql
CREATE ROLE dinebook_user WITH LOGIN PASSWORD 'choose-a-local-password';
CREATE DATABASE dinebook OWNER dinebook_user;
```

From `Backend/src`, create the environment file and install dependencies:

```bash
cp .env.example .env
../venv/bin/pip install -r requirements.txt
```

Set `DATABASE_URL` in `.env`, for example:

```env
DATABASE_URL=postgresql+psycopg://dinebook_user:choose-a-local-password@localhost:5432/dinebook
```

Apply all migrations and start the API from `Backend/src`:

```bash
../venv/bin/alembic upgrade head
../venv/bin/uvicorn app.main:app --reload
```

The API listens on `http://127.0.0.1:8000`. Interactive docs are at `/docs`, OpenAPI JSON is at `/openapi.json`, and the health endpoint is `/api/health`. Schema setup is migration-driven; starting the API does not create tables.

## Environment variables

The application reads `Backend/src/.env` and process environment variables. `.env.example` lists the supported values; configure the ones needed for your deployment.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL; required |
| `FRONTEND_URL` | Frontend origin allowed by credentialed CORS; defaults to `http://localhost:5173` |
| `FRONTEND_PUBLIC_URL` | Optional public frontend origin for shared/ngrok deployments and email links |
| `JWT_SECRET_KEY` | Signing key for session JWTs; replace the development default in deployed environments |
| `JWT_EXPIRE_MINUTES` | Session lifetime in minutes; defaults to 10,080 (seven days) |
| `AUTH_COOKIE_SECURE` | Set to `true` when serving over HTTPS |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP connection settings for verification and account emails |
| `SMTP_FROM_EMAIL`, `SMTP_USE_TLS` | Sender address and SMTP TLS behavior |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth credentials for customer sign-in |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Cloudinary credentials for document and menu image storage |

For ngrok development, expose the Vite frontend and set `FRONTEND_PUBLIC_URL` to that origin. Vite proxies `/api` to the local backend. Add the local and public callback URLs to the Google OAuth client when using Google sign-in; the local callback is `http://localhost:8000/api/auth/customer/google/callback`.

## API capabilities

All endpoints are under `/api`. The generated `/docs` page lists request models, authentication requirements, and response schemas.

| Area | Routes and behavior |
| --- | --- |
| Customer accounts | Register, verify/resend email code, sign in/out, Google OAuth, session profile, password change and recovery/reset |
| Customer discovery | List approved restaurants, read restaurant details and hours, browse categories/items with variants and images, view restaurant floor/table availability |
| Manager onboarding | Email verification, restaurant application with documents and branding/interior media, application status, login/session, profile and logout |
| Manager operations | Manage categories and dishes, availability, variants and prices, upload/delete menu images, create/manage chef staff, manage floors and tables |
| Chef portal | Login/session/logout, required first-login password change, restaurant menu details, and assigned restaurant floors |
| Platform admin | Login/session/logout, review manager applications and documents, list/inspect restaurants and menus, suspend/resume restaurants, inspect floors/tables |

Menu images accept JPEG, PNG, or WebP files up to 10 MB. The API verifies the declared media type against the image signature, scopes uploads and deletion to the manager's restaurant, and stores Cloudinary URLs plus provider IDs in `food_images`.

## Database and migrations

Alembic migrations are in `alembic/versions`. They cover shared accounts and verification, restaurants and manager applications, normalized categories/food/variants/images, soft deletion, restaurant suspension, and floor/table management. The latest migration adds Cloudinary public IDs to menu image records.

Use the same `DATABASE_URL` for Alembic and the API. From `Backend/src`:

```bash
../venv/bin/alembic current
../venv/bin/alembic upgrade head
```

## Code layout

```text
Backend/src/
├── app/
│   ├── core/          # Settings, JWT/password helpers, SMTP, Cloudinary
│   ├── db/            # SQLAlchemy engine and request sessions
│   ├── models/        # Users, restaurants, menu, floors, and tables
│   ├── routers/       # Customer, manager, chef, admin, and discovery APIs
│   └── schemas/       # Shared request/response schemas
├── alembic/versions/  # Versioned PostgreSQL schema changes
├── .env.example
├── alembic.ini
└── requirements.txt
```
