# Chef settings

`/chef/settings` is available from the Chef Portal sidebar and mobile menu. It reuses the customer portal's General Settings controls, accessible notification switches, automatic saving, loading/retry feedback, and failed-save rollback.

General settings include Light, Dark, and System Default themes; English; three date formats; 12/24-hour time; and an IANA timezone or browser default. They apply throughout the Chef Portal, including kitchen activity, order timestamps, wallet activity, notification timestamps, and service schedules. Reservation arrivals, service hours, and analytics day boundaries remain in restaurant local time; their display respects the chosen date and clock notation. The dashboard theme shortcut saves the same account preference.

Notification switches cover the in-app channel, new orders, order status changes, completed orders, successful payments, refunds, and wallet transactions. The delivery service checks these preferences before creating either a notification or its recipient-scoped realtime wake-up. Refund metadata selects the refund preference. Existing notification history remains available, and kitchen order synchronization continues independently of optional notifications. Security, account protection, legal messages, failed payments, system incidents, and restaurant suspension notices remain enabled.

Chef email currently serves essential account and invitation messages, so it has no optional delivery switch. The page explains this. These settings filter existing notification producers; they do not create additional email campaigns or scheduled jobs.

## API and storage

| Endpoint | Purpose |
| --- | --- |
| `GET /api/chef/settings` | Initialize missing preferences and return both sections. |
| `PATCH /api/chef/settings/general` | Save only supplied general fields and return confirmed settings. |
| `PATCH /api/chef/settings/notifications` | Save only supplied notification switches and return confirmed settings. |

All endpoints use `current_chef`, including active-account, temporary-password, current-session, and restaurant-suspension checks. Ownership comes from the authenticated chef; request bodies cannot select an account. Notification fields require strict booleans, general values use the shared validation, and unknown fields are rejected. Responses are uncached. Database failures roll back and return the shared safe 503 response.

General preferences reuse the existing `customer_settings` table's user foreign key. Notifications reuse `notification_preferences`; existing category opt-outs are preserved, and the legacy `/api/notifications/preferences` endpoint reads and updates the same granular values. Account-scoped frontend stores isolate chefs, managers, and customers, including late responses and sign-out/sign-in changes. Temporary-password routes do not load settings.

No new migration, dependency, or environment variable is required. Deployments need the existing `0053_customer_settings` migration or later. Restart the backend to register the new routes.

## Verification

Validated on 14 November 2026:

- Backend settings suites: 40 passing tests, including 12 new chef cases covering persistence, ownership, validation, role/password/suspension restrictions, legacy preference compatibility, failed-save rollback, essential notices, recipient-scoped events, and the real order/wallet notification producers.
- Chef dashboard and wallet notification regressions: 14 passed; 10 opt-in PostgreSQL tests skipped.
- Frontend chef settings, chef dashboard, manager settings, shared settings store, and date-formatting tests passed.
- Production build and whitespace checks passed. Vite retains its existing large-bundle advisory.
- Real browser/API checks used disposable in-memory SQLite records: keyboard switch activation, light/system theme behavior, saved date/time/timezone selections, reload persistence, dashboard formatting, route redirects, account switching, and mobile navigation. The 390px mobile layout had no horizontal overflow. Preview servers were stopped afterward.
