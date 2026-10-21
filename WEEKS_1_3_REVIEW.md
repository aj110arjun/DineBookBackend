# DineBook Weeks 1–3 bug and validation review

Reviewed on 21 October 2026. Scope: existing functionality scheduled through Week 3 in the supplied cross-portal plan. Week 4 features were excluded.

## Fixes

| Area | Changes |
| --- | --- |
| Authentication and passwords | Added the customer password-change API used by the existing form. It checks the current password and confirmation, rejects password reuse, and refreshes the current session. Password changes and resets now persist `password_changed_at`; all four portal authorization checks reject older sessions. New passwords require 10–128 characters, a letter, and a number or symbol. Customer registration/reset and manager registration enforce that baseline. |
| Chef onboarding | Operational APIs now enforce the existing first-login password-change requirement. Later password changes require the current password. The form supports both flows. This retains account provisioning by temporary password; it does not implement an invitation-token lifecycle. |
| Account status and token validation | Inactive-status customer accounts cannot sign in or retain API access even if their active flag remains true. Google sign-in checks account status too. Malformed JWT objects, invalid encoding, and non-finite timestamps are rejected. |
| Floors and tables | Update requests cannot clear non-nullable fields. Optional floor descriptions may still be cleared. |
| Menu variants | Prices use decimal validation, reject non-finite values, and allow at most two decimal places within the database range. |
| Booking hours and reservations | Schedule and booking times reject timezone offsets and seconds. Booking starts must match the 30-minute slots relative to the actual opening time. Duplicate or conflicting table selections are rejected. Cancelled/rejected food orders no longer prevent reservation completion. |
| Orders and desk payments | Repeating a mark-paid request preserves the original payment timestamp. Paying a spot order does not release its table until the order is completed; completing an already-paid order releases the table. |
| Offers | Removed duplicate preview definitions/routes. Preview and checkout consistently account for active redemption records and pending preorder claims. Reversed redemptions no longer incorrectly consume the customer's cap. Duplicate cart lines, deleted variants, zero eligible subtotal, blank titles, duplicate/mixed scope selections, and invalid date ranges are rejected. |
| Payments and webhooks | Malformed failed-payment identifiers return a validation error. Added signed-request tests for invalid signatures, amount mismatches, duplicate deliveries, and out-of-order wallet funding events. |
| Reviews | Duplicate report/review insert conflicts return HTTP 409 even when detected during flush. Owner replies lock the parent review to serialize concurrent reply creation. |
| Notifications | Wallet notifications preserve manager, chef, and admin revenue links. Admin notification events can use the authenticated, user-scoped event stream. |
| Revenue | Paid preorder revenue and commission remain counted after creating the corresponding kitchen order, while avoiding double counting the order. |
| Existing test failures | Corrected stale checkout expectations to include the configured 18% tax after the discount. The tests explicitly verify tax rate and amount. SQLite test UUID columns now use text affinity to avoid intermittent numeric conversion errors. |

## Verification

- Backend: `venv/bin/python -m unittest discover -s tests -v` — 36 tests passed. API tests use isolated in-memory SQLite and signed synthetic provider events; they do not send email or charge money.
- Frontend: production build and invoice test passed.
- Full application OpenAPI generation passed; the customer password-change endpoint is registered.
- Diff whitespace checks passed in both repositories.
- Migration `0044_password_changed_at` applied successfully to the configured local PostgreSQL `dinebook` database. Apply `venv/bin/alembic upgrade head` to any other database before running this version.
- Existing unrelated working-tree changes were preserved.

## Remaining scope and limits

This is a bug/validation pass, not certification that every Weeks 1–3 acceptance criterion is complete. Existing role and restaurant ownership guards were reviewed, but every endpoint combination and concurrent PostgreSQL transaction has not been exercised.

The planned secure staff invitation lifecycle (hashed expiring tokens, accept/reject/revoke, lifecycle tracking and admin inspection) remains absent. Notification delivery-state/retry processing and complete event/recipient coverage also remain unfinished. These require feature implementation beyond the fixes listed here.

Chef revenue currently represents manual wallet transfers, not a full payroll system. Manager revenue minus platform commission does not account for every operating expense and therefore is not full restaurant profit. Browser journeys, real SMTP delivery, live Razorpay integration and PostgreSQL concurrency behavior were not tested end to end. The frontend build retains its existing large-bundle warning.
