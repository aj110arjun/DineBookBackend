# Manager Profile

The Manager Portal now includes **Profile** above Settings in the sidebar, at `/manager/profile`. It uses the existing manager layout, JWT session, theme/date preferences, validation helpers, image cropper and confirmation component.

## Behavior

- Manager Details displays the authenticated manager's name, email, phone, avatar, role, account status and creation/update timestamps.
- Edit Profile prepopulates name and phone, supports cropping, previewing, replacing and removing a photo, and provides validation, saving/error/success feedback and cancellation. Sign-in email, role, status and account identifiers cannot be edited.
- Restaurant Details displays the manager's associated restaurant's name, description, cuisine, contact details, address, city/state/postcode, approval status, branding image, timestamps, submitted registration-document metadata and saved map location.
- The open/closed indicator reuses existing weekly schedules and exceptions in the restaurant's business timezone. Unconfigured hours are identified as such.
- Editable public restaurant details include name, description, cuisine, contact email/phone and address fields. Saving requires confirmation. The branding editor updates the primary existing branding document so restaurant listings use the new image; interior images and verification documents remain intact.
- The saved map links to the existing map-pin editor. Address text edits do not silently move coordinates. Operating-hours changes also use the existing management screen.
- Missing restaurants, images, contact information, schedules and documents have explicit empty states. Loading and API failures never fall back to invented account or restaurant data.
- Requests are cancelled when the page/account changes. Profile state follows the authenticated account ID; changing accounts discards the former account's displayed data and unsaved drafts. Duplicate submissions are guarded. When text saves but a photo fails, the saved response is retained and retry submits only the outstanding change.

## API

All endpoints use `current_manager` and derive ownership from the authenticated manager; no caller-supplied manager or restaurant ID is accepted as authorization.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/manager/profile` | One typed response containing manager and associated restaurant; restaurant is null when absent |
| PATCH | `/api/manager/profile` | Update only manager name/phone; returns saved manager details |
| POST | `/api/manager/profile/avatar` | Validated multipart `image` upload; returns saved manager details |
| DELETE | `/api/manager/profile/avatar` | Remove the manager's own avatar |
| PATCH | `/api/manager/profile/restaurant` | Partial update of permitted public restaurant fields |
| POST | `/api/manager/profile/restaurant/branding-image` | Validated multipart `image` upload for primary branding |

The existing `/api/auth/manager/restaurant/location` PUT endpoint and `/manager/restaurant-location` screen remain the map editing route. Existing operating-hours APIs are reused without changes.

Pydantic schemas forbid unknown update fields and explicit null/blank values for required listing details. Description/contact email may be cleared; personal phone may be cleared. Restaurant postal codes are limited to the actual ten-character database column. Names, international phone numbers, contact emails and text lengths follow existing conventions. Read-only stored manager emails are displayed as recorded, including legacy account addresses; no email-changing endpoint is introduced.

Responses exclude password hashes, token details, avatar storage IDs and private verification-file URLs. Registration documents expose only their type, filename, ID and submission timestamp, with no claim of per-document verification. Restaurant queries, document queries and update locks are scoped to the manager's restaurant. Suspended restaurants and invalid, revoked or non-manager sessions follow existing authorization behavior.

## Storage and images

No migration, new table or new database column is required. Existing `User` avatar fields, `Restaurant` fields and `RestaurantDocument.BRANDING_IMAGE` records are reused.

Personal photo handling reuses `customer_profile`'s image validation, row locks, rollback and storage cleanup, with a manager-specific Cloudinary folder. Uploads accept JPEG/PNG/WebP, at most 5 MB and 20 megapixels. The backend decodes/verifies content rather than trusting its MIME header. Restaurant branding uses the same validation, validates the provider response and deletes the newly uploaded asset if its database write fails.

Normal image storage requires the already supported `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY` and `CLOUDINARY_API_SECRET` configuration. Missing configuration returns a clear 503. Tests mock storage; no real Cloudinary uploads were performed.

The existing restaurant document schema has no Cloudinary public-ID column. Replaced branding assets are retained in storage rather than inferring an ID and risking deletion of an unrelated file. Routine cleanup of detached historical branding assets remains a storage-administration task.

## Schema limits

- There are no separate restaurant logo/banner roles. The first existing branding document is shown as the cover; no logo record is fabricated.
- There is no country or registration-number column. The page displays supported address fields and submitted-document metadata instead.
- There is no approved email-change workflow bound to a manager session, so manager sign-in email remains read-only. The restaurant's public contact email is editable independently.
- Approval/account statuses and verification documents remain admin controlled.
- Submitted documents are not relabeled as verified. Operating hours are not inferred when no schedule is configured.

## Validation

Commands from their corresponding repository roots:

```sh
# Backend
venv/bin/python -m unittest tests.test_manager_profile tests.test_customer_profile tests.test_manager_settings tests.test_customer_settings -q

# Frontend
npm run test:manager-profile
node --test src/lib/*.test.js src/state/*.test.js
npm run build
```

Results: 54 backend profile/account regression tests passed. The frontend suite passed all 94 tests; focused profile/customer profile checks passed all 20 tests after final UI refinements. The production build passed with the application's existing bundle-size advisory.

Headless Chromium also verified real React/API integration against an isolated SQLite fixture with mocked image storage: prepopulation, edit/cancel, invalid inputs, personal saves, crop/preview/upload, public restaurant confirmation, branding updates, reload persistence, read-only controls and authenticated-account switching. Desktop 1440px, tablet 768px and mobile 390px had no page overflow. Dark mode and circular avatars were visually checked, with no JavaScript runtime exceptions. Test fixtures did not modify live manager or restaurant records.

## Files for this change

Backend created:

- `app/routers/manager/manager_profile.py`
- `app/schemas/manager_profile.py`
- `app/services/manager_profile.py`
- `tests/test_manager_profile.py`
- `docs/manager-profile.md`

Backend modified:

- `app/main.py` — register profile endpoints.
- `app/services/customer_profile.py` — share validated image handling with a configurable manager upload folder and image label; customer defaults remain unchanged.
- `.gitignore` — include the profile test module.

Frontend created:

- `src/pages/manager/ManagerProfilePage.jsx`
- `src/components/manager/profile/ManagerInformation.jsx`
- `src/components/manager/profile/RestaurantInformation.jsx`
- `src/components/manager/profile/ProfileParts.jsx`
- `src/assets/css/ManagerProfilePage.css`
- `src/lib/managerProfile.js`
- `src/lib/managerProfile.test.js`
- `src/lib/managerProfile.render.test.js`

Frontend modified:

- `src/App.jsx` — protected profile route.
- `src/components/manager/ManagerLayout.jsx` — sidebar link.
- `src/components/preferences/AccountPreferences.jsx` — expose current account identity for profile state isolation.
- `src/lib/customerProfile.js` — share the established save flow with configurable profile endpoint paths; existing customer imports remain supported.
- `package.json` — focused profile test command.

The preceding manager Settings implementation remains in the workspace and is preserved.
