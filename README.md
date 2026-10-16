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

## Operating hours and availability

Manager endpoints under `/api/manager/operating-hours` manage weekly schedules and date exceptions. Chef service hours are read-only at `/api/chef/service-hours`; public weekly/date-specific hours are available at `/api/customer/restaurants/{restaurant_id}/operating-hours`; administrators can inspect schedules and slots under `/api/admin/restaurants/{restaurant_id}/`. Existing single-window weekday schedules remain valid until a manager saves the expanded schedule. Exceptions replace weekly hours for their full date range, and overlapping exceptions or periods are rejected. A schedule change that conflicts with active reservations requires explicit confirmation; reservation records remain intact.

Availability preserves the existing Asia/Kolkata timezone, 30-minute slot interval, and 90-minute reservation duration. Newly configured periods must start and end on the same local date; overnight periods are not accepted by the schedule editor. Apply the new schema with `cd Backend/src && ../venv/bin/alembic upgrade head`.

## Environment variables

The application reads `Backend/src/.env` and process environment variables. `.env.example` lists the supported values; configure the ones needed for your deployment.

Responses larger than 1 KiB are gzip-compressed when the client advertises gzip support. Server-sent event streams are left uncompressed so events can be delivered as they are produced.

API request middleware counts body bytes as they arrive, rejects unsupported HTTP methods, logs request IDs/status/duration without recording request data, and returns a generic error with the request ID for unexpected API failures. Rate limits use PostgreSQL counters shared across API workers: login (10/minute), account recovery (6/minute), other authentication writes (20/minute), booking/payment writes (30/minute), restaurant discovery (180/minute), other writes (180/minute), and other API reads (600/minute), per ASGI client IP and fixed 60-second windows. Razorpay webhooks and event streams use dedicated handling and are excluded from general limits. Configure Uvicorn to trust forwarded client addresses only from your known reverse proxy; otherwise requests behind the proxy share the proxy IP for rate limiting. Old counters are periodically pruned. Limited requests return `429` with `Retry-After`; if PostgreSQL counters are unavailable, requests covered by a limit return `503`.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL; required |
| `FRONTEND_URL` | Frontend origin allowed by credentialed CORS; defaults to `http://localhost:5173` |
| `FRONTEND_PUBLIC_URL` | Optional public frontend origin for shared/ngrok deployments and email links |
| `APP_ENV` | Use `development` locally and `production` in deployment; production startup rejects weak JWT secrets and insecure auth cookies |
| `JWT_SECRET_KEY` | Random signing key of at least 32 characters; required in production |
| `JWT_EXPIRE_MINUTES` | Session lifetime in minutes; defaults to 10,080 (seven days) |
| `AUTH_COOKIE_SECURE` | Set to `true` when serving over HTTPS |
| `MAX_REQUEST_BODY_BYTES` | Maximum accepted request body size; defaults to 20 MiB and must be at least 1 KiB |
| `MAX_JSON_BODY_BYTES` | Maximum accepted JSON and form request body; defaults to 1 MiB. Multipart uploads use `MAX_REQUEST_BODY_BYTES`. |
| `RATE_LIMIT_ENABLED` | Enables PostgreSQL-backed API limits; defaults to `true`. Apply migrations before serving API traffic. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP connection settings for verification and account emails |
| `SMTP_FROM_EMAIL`, `SMTP_USE_TLS` | Sender address and SMTP TLS behavior |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth credentials for customer sign-in |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Cloudinary credentials for document and menu image storage |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | Razorpay Standard Checkout credentials for customer wallet top-ups; the secret remains server-side |
| `RAZORPAY_WEBHOOK_SECRET` | Server-only secret configured on the Razorpay Dashboard for the webhook endpoint |
| `RAZORPAY_WEBHOOK_PREVIOUS_SECRET` | Optional previous secret during webhook secret rotation; remove after old retries have expired |
| `ORDER_TAX_PERCENT` | GST percentage applied to food-order totals after discounts; defaults to `18` |

For ngrok development, expose the Vite frontend and set `FRONTEND_PUBLIC_URL` to that origin. Vite proxies `/api` to the local backend. Add the local and public callback URLs to the Google OAuth client when using Google sign-in; the local callback is `http://localhost:8000/api/auth/customer/google/callback`.

## API capabilities

All endpoints are under `/api`. The generated `/docs` page lists request models, authentication requirements, and response schemas.

Customer wallet top-ups create Razorpay orders on the server and credit the wallet only after the payment signature and captured payment amount are verified. Configure Razorpay test keys in `Backend/src/.env` for local testing, and apply all migrations with `venv/bin/alembic upgrade head` before using wallet top-ups.

## Webhooks and real-time events

Razorpay sends server-to-server callbacks to `POST /api/webhooks/razorpay`. Configure a webhook secret in Razorpay Dashboard and set the same value in `RAZORPAY_WEBHOOK_SECRET`. Subscribe only to `payment.captured` for wallet funding; do not also subscribe to `order.paid` for the same payment unless a separately deduplicated use case is added. The handler validates `X-Razorpay-Signature` against the exact raw request bytes using HMAC-SHA256 and requires `X-Razorpay-Event-Id`. It credits only a matching, correctly priced pending wallet top-up. The provider event ID and resulting wallet transaction are committed together; duplicate event deliveries are acknowledged without a second credit. Raw webhook bodies and payment details are not retained or logged.

Customer, manager, and chef order pages plus the customer wallet use authenticated Server-Sent Events at `GET /api/events/stream`. The existing HttpOnly session cookie authenticates the request; the event endpoint checks the portal role, active account, allowed frontend origin, and customer or restaurant scope. Admin support lookup remains read-only and does not subscribe. Events are persisted with the associated order, reservation, or top-up transaction and delivered only after commit. The stream emits a sanitized schema:

```json
{"event_id":"uuid","event_type":"order.status_changed","schema_version":1,"occurred_at":"ISO-8601","resource_id":"resource-id","data":{"status":"READY","payment_status":"PAID"}}
```

Current event names are `order.status_changed`, `booking.updated`, and `payment.status_changed`. Their `data` objects contain status fields only. Clients refresh their authorized REST view when an event arrives and after reconnect, so an interrupted stream does not become the source of truth. SSE is one-way; no client messages or WebSocket subscriptions are accepted. The stream polls the persisted event table every two seconds, so it works across API workers without Redis. Keep the API endpoint directly reachable from the frontend origin with credentialed CORS enabled; configure a reverse proxy not to buffer `text/event-stream` responses.

Apply migration `0021_realtime_webhooks` before deploying this API. Keep webhook receipts as the deduplication ledger; do not delete them while Razorpay can retry an event. `realtime_events` currently has no automatic pruning job; schedule a reviewed retention cleanup (for example, retain 30 days) after ensuring client reconnects reconcile from REST. Keep `RAZORPAY_WEBHOOK_PREVIOUS_SECRET` during rotation so retries signed with the previous secret continue to validate.

For local webhook tests, use Razorpay Test mode and a publicly reachable staging/tunnel URL accepted by Razorpay. Never expose the webhook secret in the frontend or include it in logs. Check server logs for event IDs and processing errors; logs intentionally exclude raw payloads and credentials.

| Area | Routes and behavior |
| --- | --- |
| Customer accounts | Register, verify/resend email code, sign in/out, Google OAuth, session profile, password change and recovery/reset |
| Customer discovery | List approved restaurants, read restaurant details and hours, browse categories/items with variants and one shared food image, view restaurant floor/table availability |
| Manager onboarding | Email verification, restaurant application with documents and branding/interior media, application status, login/session, profile and logout |
| Manager operations | Manage categories and dishes, availability, variants and prices, upload/replace/remove each dish's single image, create/manage chef staff, manage floors and tables |
| Chef portal | Login/session/logout, required first-login password change, restaurant menu details, and assigned restaurant floors |
| Platform admin | Login/session/logout, review manager applications and documents, list/inspect restaurants and menus, suspend/resume restaurants, inspect floors/tables |

### Reviews and moderation

Reviews target a restaurant (one review per customer per restaurant) and are tied to a completed customer food order for eligibility. The API uses the existing role sessions:

| Portal | Endpoints |
| --- | --- |
| Customer | `GET/POST /api/customer/reviews`, `PATCH /api/customer/reviews/{review_id}`, `POST /api/customer/reviews/{review_id}/flags` |
| Public | `GET /api/reviews/restaurants/{restaurant_id}` (published reviews and published owner replies only) |
| Chef | `GET /api/chef/reviews` (assigned restaurant only) |
| Restaurant manager | `GET /api/manager/reviews`, `PUT /api/manager/reviews/{review_id}/reply` |
| Platform admin | `GET /api/admin/reviews`, `POST /api/admin/reviews/{review_id}/actions` |

Moderation actions are `APPROVE`, `REJECT`, `HIDE`, `RESTORE`, and `DISMISS_FLAG`. Rejection, hiding, and restoration require a reason. Review and flag state are stored independently, and review status changes create an immutable audit event. Apply migration `0031_reviews_moderation` before deploying.

## Offers

Migration `0029_offer_applicability` extends the existing `food_order_offers` table and creates `offer_redemptions`. Existing coupon-backed orders are copied into the ledger, with cancelled/rejected orders marked `REVERSED`; the offer usage counters are reconciled from active redemptions. Order totals already store the charged subtotal, discount, and total, so later offer edits do not reprice historical orders.

Managers use `GET /api/manager/orders/offers` (search/status/sort filters and offset pagination), `GET /api/manager/orders/offers/{id}`, `GET /api/manager/orders/offers/summary`, `POST /api/manager/orders/offers`, `PUT /api/manager/orders/offers/{id}`, and `PATCH /api/manager/orders/offers/{id}?is_active=false`. `GET /api/manager/orders/offers/{id}/redemptions` returns scoped redemption history. Item and category IDs are checked against the manager's restaurant. There is no hard-delete operation.

Customers can discover offers with `GET /api/customer/orders/offers?restaurant_id={id}` and validate a proposed code using `POST /api/customer/orders/offers/validate` with `{ "reservation_id": "uuid", "offer_code": "SAVE10", "items": [{"variant_id":"uuid","quantity":2}] }`. Validation does not reserve capacity. At order creation, the server rereads current menu prices and validates the offer again. `offer_code` on the existing order checkout request remains the application mechanism.

Platform administrators have read-only `GET /api/admin/orders/offers`, `GET /api/admin/orders/offers/{id}`, `GET /api/admin/orders/offers/statistics`, `GET /api/admin/orders/offer-redemptions`, and `GET /api/admin/orders/offer-redemptions/{id}` endpoints. They do not receive offer mutation endpoints. Responses identify customers only by internal ID.

Only one offer applies to an order. Restaurant scope counts all merchandise; item/category scope counts only matching menu items. An item in multiple selected categories is counted once because order lines are unique by variant and matching is boolean. Minimum spend is checked against that eligible merchandise subtotal. Percentage discounts are capped by `max_discount`; fixed discounts and all other discounts are capped at the eligible subtotal. The configured `ORDER_TAX_PERCENT` is calculated after the offer discount and stored with the order; older orders retain their original stored amounts. Offer timestamps must include a timezone; the UI sends UTC instants. Checkout locks the offer row, checks global and per-customer limits, inserts the order and redemption in one database transaction, and is idempotent through the existing customer checkout key. Customer or staff cancellation/rejection marks the redemption reversed and releases capacity. Payment-provider reversals remain governed by the existing payment workflow.

Food images accept JPEG, PNG, or WebP files up to 10 MB. The API verifies the declared media type against the image signature, scopes uploads and deletion to the manager's restaurant, and stores one Cloudinary URL plus provider ID per dish in `food_images`. Food menu responses use `image_url`; variant responses contain variant data only.

## Database and migrations

Alembic migrations are in `alembic/versions`. They cover shared accounts and verification, restaurants and manager applications, normalized categories/food/variants/images, soft deletion, restaurant suspension, floor/table management, reservations, orders, and wallet top-ups. Migration `0021_realtime_webhooks` adds the transactional event log and webhook receipt ledger.

Migration `0023_single_food_image` keeps the earliest food-level image for each dish, promotes the earliest variant image only when no food-level image exists, and enforces one image record per dish. Other legacy image records are detached from the menu, but their Cloudinary files are not automatically removed. Newly created order and preorder snapshots include the food image URL; historical snapshots remain unchanged.

## Notifications

Migration `0032_notifications` creates recipient-scoped persistent notifications and optional per-category preferences. Apply it with `../venv/bin/alembic upgrade head` before using the notification APIs. The APIs use the existing HTTP-only `dinebook_access_token` cookie and are available at `GET /api/notifications` (with `limit`, `offset`, `unread`, `category`, and `priority` filters), `GET /api/notifications/unread-count`, `GET /api/notifications/{id}`, `PATCH /api/notifications/{id}/read`, `PATCH /api/notifications/read-all`, `DELETE /api/notifications/{id}`, and `GET/PATCH /api/notifications/preferences`.

Live updates use the existing cookie-authenticated database-backed SSE endpoint, `GET /api/events/stream`; no WebSocket or Redis dependency is required. Notification records and their recipient-only SSE wake-ups are written in the same SQL transaction as supported order, reservation, payment, review, and approval changes. The SSE cursor allows clients to resume after disconnects; the REST API remains the source of truth. The React portals reconnect and refresh notifications after a new event or connection recovery.

Reminder scheduling, staff account lifecycle alerts, payment reconciliation incidents, and the complete breadth of operational/admin events are not yet wired to a persistent job queue or event source. No scheduler or Redis dependency is configured in this project, so upcoming reservation reminders are not generated automatically.

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
