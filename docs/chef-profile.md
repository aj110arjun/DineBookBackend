# Chef Profile

The Chef Portal includes **Profile** above Settings in the sidebar and mobile menu, at `/chef/profile`.

## Behavior

- Displays the authenticated chef's name, phone, sign-in email, photo, role, account status, and creation/update timestamps.
- Edit Profile supports validated name/phone changes, optional phone clearing, photo selection/cropping/preview, and confirmed staged photo removal. Cancel restores saved values. Email and account-control fields remain read-only.
- Successful responses immediately update the page and chef sidebar identity. `/api/chef/me` includes phone and avatar URL so reloads and periodic session checks preserve the saved identity.
- Assigned Restaurant shows the chef's current workplace, cuisine, address, contact phone, status, and a link to service hours. Restaurant details and assignments are read-only for chefs. An unassigned account has an explicit empty state.
- Loading, retry, validation, saving, success, and API-error states use existing portal components. Dates follow account preferences; styling supports light/dark themes and mobile layouts.
- Account-scoped page state and keyed editors discard prior-account data and drafts. Unmounting aborts pending reads/saves; identity updates check the account ID before applying a response. Duplicate submissions are blocked. If details save but a photo fails, the confirmed details remain visible and retry only submits the remaining change.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/chef/profile` | Return the authenticated chef and their assigned restaurant, or a null restaurant. |
| PATCH | `/api/chef/profile` | Update only name and phone; return confirmed chef details. |
| POST | `/api/chef/profile/avatar` | Upload multipart `image`; return confirmed chef details. |
| DELETE | `/api/chef/profile/avatar` | Remove the authenticated chef's photo. |

All endpoints use `current_chef`, retaining current-session, account-status, temporary-password, and restaurant-suspension restrictions. Ownership is derived from authentication. Update schemas forbid extra fields; clients cannot change IDs, email, role, status, passwords, avatar storage IDs, restaurant details, or assignment. Typed responses exclude secrets and restaurant verification documents. Profile responses use the existing no-store API policy.

## Reuse and storage

The frontend shares the established manager profile editor through `components/profile/AccountInformation.jsx`. The manager wrapper preserves its existing API and labels. Personal-field validation, staged saves, image cropping, image previews, and profile detail controls are reused. Profile crop dialogs are bounded to the viewport so square photo crops keep their action controls accessible.

The backend reuses the existing `User` fields and validated profile/image services. No migration, dependency, or new environment variable is needed. Chef uploads use `dinebook/chef-avatars/<authenticated-user-id>` and the existing Cloudinary configuration (`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`). Missing storage configuration returns a clear 503. Photo validation, 5 MB/20 megapixel limits, rollback, replacement cleanup, and removal use the existing shared implementation.

Restart a backend running without auto-reload to register the new routes.

## Verification

Validated on 14 November 2026:

- `venv/bin/python -m unittest tests.test_chef_profile tests.test_customer_profile tests.test_manager_profile tests.test_chef_settings -q`: 48 passed, including 10 new chef profile tests. Covers persistence, ownership, strict validation, read-only restrictions, legacy emails, unassigned chefs, session/role/password/suspension checks, image validation/storage paths, session identity, removal, and rollback cleanup.
- `npm run test:chef-profile`: chef rendering/save-path tests and existing customer/manager profile regression tests passed.
- `npm run build` and `git diff --check`: passed. Vite retains the existing bundle-size advisory.
- Browser verification used isolated disposable SQLite data and mocked photo storage: edit/cancel, invalid input, saved name/phone, immediate sidebar identity, reload persistence, crop/preview/upload/removal, account switching with an unsaved draft, light/dark appearance, mobile navigation, and a 390px viewport with no horizontal overflow. The mocked saved image URL intentionally exercises the missing-image fallback; no real Cloudinary uploads or production records were changed. Preview servers were stopped after verification.
