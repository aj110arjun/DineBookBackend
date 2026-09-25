# DineBook Backend — Customer Registration

FastAPI customer registration backed by PostgreSQL, SQLAlchemy 2, and Alembic. The backend lives in this folder and uses the existing `venv/` virtual environment.

## Included

- `POST /api/auth/customer/register` validates name, email, password, and confirmation.
- Email addresses are normalized to lowercase and protected by a database unique index.
- Passwords are hashed with Argon2; plaintext passwords and hashes are never returned by the API.
- New customer records use the shared `users` table and remain inactive until the email confirmation code is verified.
- Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, and optionally `SMTP_USE_TLS` in the backend environment to deliver confirmation codes. Codes expire after 10 minutes.
- Apply database changes with `venv/bin/alembic upgrade head` from `Backend/`.
- CORS accepts the configured frontend origin with credentials enabled.
- Alembic owns schema changes; the app does not call `Base.metadata.create_all()`.

## 1. Create a PostgreSQL database

PostgreSQL must be installed and running. Connect as a PostgreSQL administrator and create an application role and database:

```sql
CREATE ROLE dinebook_user WITH LOGIN PASSWORD 'set-a-local-password';
CREATE DATABASE dinebook OWNER dinebook_user;
```

Run those statements using your PostgreSQL administration method, such as `sudo -u postgres psql`. Choose a local password and use the same value in `DATABASE_URL` below.

## 2. Configure and install

From `Backend/`:

```bash
cp .env.example .env
```

Edit `.env` and set `DATABASE_URL` to your local role/password. Keep `.env` private; it is ignored by Git.

The virtual environment is already present. Install the backend packages into it:

```bash
venv/bin/pip install -r requirements.txt
```

## 3. Apply the migration and start the API

```bash
venv/bin/alembic upgrade head
venv/bin/uvicorn app.main:app --reload
```

The API documentation is available at `http://localhost:8000/docs`; health check: `http://localhost:8000/api/health`.

The API and Alembic both read `DATABASE_URL` from `Backend/.env` (or the environment). Always run the migration against the same database URL used by the API. If registration reports that the schema is not initialized, confirm PostgreSQL is running and run `venv/bin/alembic upgrade head` from `Backend/`, then restart the API. The migration creates the shared `users` table; starting the API does not create tables automatically.

## Register a customer

```http
POST /api/auth/customer/register
Content-Type: application/json
```

```json
{
  "name": "Mia Sharma",
  "email": "mia@example.com",
  "password": "choose-a-strong-password",
  "confirm_password": "choose-a-strong-password"
}
```

A successful registration returns `201 Created` with the new customer's public profile. Duplicate emails return `409 Conflict`; malformed input or password mismatch returns `422 Unprocessable Entity`.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL (`postgresql+psycopg://...`) | Local `dinebook` role/database URL |
| `FRONTEND_URL` | Allowed credentialed CORS origin | `http://localhost:5173` |

## Structure

```text
Backend/
├── alembic/versions/0001_customer_users.py  # Initial PostgreSQL migration
├── app/
│   ├── core/                                # Settings and Argon2 utilities
│   ├── db/                                  # SQLAlchemy engine and session dependency
│   ├── models/user.py                       # Shared roles/statuses and User model
│   ├── routers/customer_auth.py             # Customer registration endpoint
│   ├── schemas/customer.py                  # Registration input/public output
│   └── main.py                              # FastAPI app and CORS
├── requirements.txt
└── venv/
```
