# Admin settings

The signed-in administrator can manage personal preferences at `/admin/settings`.
The Account sidebar links to the page. Changes save automatically and are scoped
to the authenticated admin; they do not configure other administrators or the
platform's business rules.

The API exposes `GET /api/admin/settings`, `PATCH /api/admin/settings/general`,
and `PATCH /api/admin/settings/notifications`. PATCH requests accept partial
updates and return both sections. Every endpoint uses `current_admin` and rejects
unknown fields, invalid general choices, and non-boolean notification values.

General preferences cover light/dark/system appearance, English language,
date/time formatting, and a validated IANA timezone or browser default. Activity
timestamps follow that preference; reservation wall-clock dates and the
dashboard's business reporting periods retain their existing meanings.

Optional in-app notifications can be paused globally or by restaurant requests,
review reports, wallet activity, and system updates. Filtering happens before
notification persistence and recipient-scoped realtime events. Existing history
remains available. Security, account protection, legal notices, failed payments,
restaurant suspension, and system incidents bypass optional switches. These
settings do not add an optional email channel or new notification producers.

Storage reuses the existing `customer_settings` user foreign key and
`notification_preferences`. Existing category opt-outs are preserved; the legacy
`/api/notifications/preferences` API stays synchronized with admin settings.
The frontend creates a separate store for each authenticated account and clears
the admin scope on sign-out, authentication failure, or leaving its route guard.

No new migration, dependency, or environment variable is required. The database
must already have migration `0053_customer_settings` or later. Restart the API
after adding the router.

From `DineBookBackend`, run:

```bash
venv/bin/python -m unittest tests.test_admin_settings tests.test_customer_settings tests.test_manager_settings tests.test_chef_settings -q
```

From the frontend `DineBook` directory, run `npm run test:admin-settings` and
`npm run build`.
