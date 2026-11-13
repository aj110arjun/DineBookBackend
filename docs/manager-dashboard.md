# Restaurant manager dashboard

The existing React dashboard now uses a single authenticated, read-only analytics request. Its summary cards, revenue bars, status donut, floor cards, popular items and operational lists retain the existing manager layout. Additional panels show order and reservation trends, customer cohorts, payment methods and published reviews. All production values come from database queries; fixtures are confined to tests.

## API

`GET /api/manager/dashboard/overview`

Query parameters:

| Parameter | Values / behavior |
| --- | --- |
| `start_date`, `end_date` | Inclusive business dates, ISO `YYYY-MM-DD`; supply both or neither. Defaults to the last 30 days including today. |
| `group_by` | `auto`, `day`, `week`, `month`, `year` |
| `popular_sort` | `quantity` (default) or `revenue`; returns up to 10 variants |

Example: `/api/manager/dashboard/overview?start_date=2026-11-01&end_date=2026-11-30&group_by=day&popular_sort=revenue`.

Validation returns HTTP 422 for reversed or invalid dates, a single missing bound, unknown query parameters (including restaurant IDs), invalid options, dates outside 2000–2099, ranges longer than 3660 days, or daily grouping longer than 366 days. `auto` uses daily buckets through 62 days, weekly through 366, monthly through 1096, and yearly thereafter.

The typed response contains `period`, `overview`, `revenue`, `revenue_chart`, `operations`, `popular_items`, `customers`, `payment_methods`, `reviews`, `live` and explanatory `notes`. Decimal monetary values are JSON strings; the frontend converts them only for formatting and chart positioning.

The existing JWT cookie dependency `current_manager` verifies manager role, account state, verification, token validity and restaurant suspension. `get_manager_restaurant` resolves the restaurant from the authenticated manager. There is no restaurant selector or caller-supplied scope. Every aggregate and bounded list uses that restaurant ID, including joins to payment sources, floors, table assignments and menu records. No customer email, phone, wallet balance or authentication fields are returned. Upcoming bookings use booking references instead of guest identities.

## Dates and comparisons

The project has no per-restaurant timezone field. The dashboard reuses the existing business timezone, `Asia/Kolkata`, from `table_availability.py`. Timestamp filters convert local midnight to UTC and use half-open `[start, end + one day)` boundaries. This avoids a dependency on the database connection's timezone. Scheduled reservation dates are filtered directly.

| Dataset | Event date |
| --- | --- |
| Order totals, trends, average order value, popular items | `FoodOrder.created_at` |
| Reservations, guests, booking peaks | `Reservation.reservation_date`, with `start_time` for arrival-hour peaks |
| Collections and payment methods | `PlatformCommission.occurred_at` |
| Refunds and commission reversals | `PlatformCommission.refunded_at` |
| Reviews | `Review.created_at`, restricted to reviews currently `PUBLISHED` |
| Customer history | Local order creation date or scheduled eligible reservation date |
| Live floor, upcoming bookings, active orders | Current business time; independent of the historical filter |

The previous period contains exactly the same number of calendar days immediately before the current start. Percentage change is `(current - previous) / abs(previous) * 100`, rounded to two decimals. When both values are zero, change is zero; when only the previous value is zero, change is null and the UI shows “No previous baseline.”

Weeks begin on Monday. Month and year buckets begin on the first day. Edge buckets include only events inside the selected range, even when the bucket label precedes its start. Every trend is zero-filled. Negative cash-flow buckets remain visible.

## Calculations

| Metric | Definition |
| --- | --- |
| Gross collected | Sum of `PlatformCommission.gross_amount` for collections in the period, for `FOOD_ORDER`, `RESERVATION_FEE`, `PREORDER` |
| Refunds | Sum of the original gross amount for those sources reversed in the period |
| Revenue after refunds | Gross collected minus refunds |
| Commission after reversals | Collected commissions minus original commissions reversed in the period |
| Net after commission | Revenue after refunds minus commission after reversals |
| Total orders / reservations | All rows in the selected event-date range, with actual status counts |
| Average order value | Sum of `FoodOrder.total` divided by qualifying order count: paid, not cancelled/rejected, and no recorded direct order payment reversal |
| Popular items | SQL expansion of saved `FoodOrder.items` JSON snapshots from qualifying paid orders, grouped by stable variant identity; quantity sums and proportional saved-discount allocation on line totals |
| Item sales revenue | `sum(line_total * (subtotal - discount_amount) / subtotal)`; excludes tax and commission; rounds the aggregate to cents |
| Unique customers | Distinct non-null customer IDs across eligible restaurant orders and reservations |
| Eligible customer orders | Not cancelled/rejected and payment status not `REFUNDED`; placed unpaid orders represent customer activity but do not contribute paid item sales or AOV |
| Eligible customer reservations | `CONFIRMED`, `SEATED`, `COMPLETED` |
| New customers | First eligible restaurant activity across full available history falls within the selected period |
| Returning customers | Eligible activity in the selected period and an earlier eligible restaurant activity before the period |
| Returning percentage | Returning / distinct period customers * 100; this is an activity-based returning share, not a longitudinal retention cohort |
| Completion rates | `COMPLETED` / all period orders or reservations * 100, including cancelled rows in the denominator |
| Average party size / expected guests | Average / sum of guest counts for confirmed, seated and completed reservations in the period |
| Rating | Average currently published review rating in period; hidden, removed, rejected and pending reviews excluded |
| Occupancy | Recorded occupied active tables / active table total * 100; includes maintenance tables in total |

An item rename does not split the variant into multiple ranks. Snapshot names remain usable after menu soft deletion; the label is a deterministic saved name. Soft-deleted menu records do not erase historical sales. Ties in item rankings use a stable identity ordering.

Financial calculations use `Decimal`, SQL numeric aggregates, and the existing half-up `money` helper. Collections are taken exclusively from `PlatformCommission`, whose `(source_type, source_id)` uniqueness represents one actual collected payment. The dashboard does not sum wallet transactions, wallet top-ups, redemption debits, commission payouts or order totals to derive collected revenue. Payment-method joins are one-to-one source lookups without line-item or table-assignment fan-out. Unknown source methods are shown as `UNKNOWN`.

A reservation fee and preorder are separate payment sources. A materialized preorder kitchen order does not add another financial collection. Refunds are recognized when reversed, rather than silently rewriting the original collection period: a refund of an older collection can make the current period negative. A collection refunded after the report end remains a collection in that historical period. Gross captured payment-method volume is intentionally before refunds; the financial summary shows refunds separately. Net revenue is after commission, not operating profit or a tax-exclusive accounting measure.

## Live data and refresh

Live seating queries active floors and non-inactive tables. Maintenance takes precedence. An explicitly `SEATED` booking marks its assigned table occupied until its lifecycle is completed. Manual occupied/reserved states apply only to the local day they were updated. A confirmed or pending booking blocks a table as reserved only while its actual scheduled interval overlaps now, including overnight intervals. Counts never derive occupancy from aggregate historical booking totals.

The floor preview is limited to 20 tables; upcoming bookings and active orders to five each; recent published reviews to five. Links lead to the existing management screens. Previews are deterministic and do not load all transactional rows.

The frontend fetches on opening, applied date/interval/sort changes, and manual refresh. Focus/visibility return refreshes after 30 seconds; existing shared SSE order/booking/payment events are debounced by 600 ms, with reconnect refresh. No new realtime infrastructure or polling is added. Identical in-flight requests coalesce, changed queries abort old requests, stale responses cannot overwrite new results, and unmount cancels delivery. Failed requests show errors and retry, with no synthetic data fallback. Native SVG bars and dynamic CSS donuts have labels, legends, tooltips, horizontal overflow support and dark-theme colors. All-empty datasets show explanatory states instead of nonfunctional charts.

## Performance and migration

All totals, cohorts, JSON item expansion, grouping and rankings execute in SQL. Python processes only bounded grouped results and bounded live previews. The SQL query count is fixed rather than increasing with orders/customers; tests compare empty and populated fixtures. There are no per-customer, per-food or per-booking queries.

New migration `0054_manager_dashboard_indexes` adds four indexes:

- `platform_commissions (restaurant_id, occurred_at)`
- `platform_commissions (restaurant_id, refunded_at)`
- `food_orders (restaurant_id, customer_id, created_at)`
- `reservations (restaurant_id, user_id, reservation_date)`

Existing restaurant/date/status, review, floor and assignment indexes are reused. No tables or historical migrations change. Deploy the forward migration with `venv/bin/alembic upgrade head` through the normal backend rollout; this implementation does not migrate the application's live schema or change historical records. The endpoint also works before the indexes are installed, with potentially slower large-history queries.

## Validation

Run from the corresponding repository root:

```sh
# Backend: SQLite fixtures and API authorization/validation tests
venv/bin/python -m unittest tests.test_manager_dashboard -v

# Opt-in PostgreSQL: same aggregation fixtures in a unique schema, rolled back
DASHBOARD_TEST_POSTGRES=1 venv/bin/python -m unittest tests.test_manager_dashboard -v

# Complete backend regressions, optionally including PostgreSQL fixtures
DASHBOARD_TEST_POSTGRES=1 venv/bin/python -m unittest discover -s tests -v

# Frontend request, formatting, SSR component and empty/error-state tests
npm run test:dashboard
npm run build
```

PostgreSQL fixtures create a unique schema inside a transaction and roll the entire schema back; no production rows or wallets are edited. Migration tests exercise upgrade/downgrade index operations against fixtures. Frontend component tests render a saved response generated by the backend fixtures, using the existing Vite and React runtimes without new dependencies. Browser interaction/viewport QA is separate from these render tests.

Verified implementation results:

- All 29 dashboard backend tests passed, including 13 PostgreSQL aggregate/migration tests, 13 SQLite equivalents and three API/authentication tests.
- All 75 frontend regression tests passed. The 12 dashboard tests were rerun successfully after final chart fixes.
- The production Vite build passed. It retains the application's large-bundle advisory.
- Headless Chromium loaded the built React app against an isolated SQLite-backed API fixture. Today and custom date ranges changed the API request and displayed period; manual refresh requested new data. Desktop (1440px), mobile (390px) and dark mode rendered without page overflow or JavaScript runtime exceptions.
- Full backend discovery ran 221 tests: 213 passed, three skipped, one failed and four errored. The five failures were independently reproduced using an archive of unchanged HEAD application sources: an obsolete opening-time assertion, three legacy booking tests calling the changed reservation signature positionally, and an ignored seed-demo test importing the absent `app.seed_demo` module. These unrelated tests and application behavior were left unchanged.

## Known schema limitations

- The financial ledger records full source reversals, not partial refund amounts. The dashboard follows the existing full-refund lifecycle and does not invent partial refunds.
- Historical collections without commission records cannot be reconstructed reliably. They are excluded and disclosed in the UI methodology. The existing reconciliation tooling remains a separate, deliberate operation.
- Anonymous walk-ins have no stable customer ID and cannot contribute unique/new/returning customer counts. Deleted or missing activity history limits cohort accuracy.
- Historical status transitions, historical seating utilization and a reliable no-show duration are not stored as analytics snapshots. Status distributions show current row status grouped by the appropriate event date. The dashboard does not fabricate preparation durations or historical occupancy.
- Saved preorder items contribute item rankings only once a kitchen `FoodOrder` exists. Their collected funds can already appear in revenue before that point.
- Customer new/returning chart classification is per interval. A customer can be new in one interval and returning later, so interval totals should not be summed as period-unique customers.
- Review creation is available, but a distinct publication timestamp is not. Moderation changes may alter historical visible-review totals.
- The business timezone is shared across the existing application rather than configured per restaurant.

## Changed files

Backend:

- `.gitignore`: include the new dashboard test module.
- `app/main.py`: register the manager dashboard router.
- `app/routers/manager/manager_dashboard.py`: authenticated consolidated endpoint.
- `app/schemas/manager_dashboard.py`: bounded query validation and typed response schemas.
- `app/services/manager_dashboard.py`: scoped SQL analytics and bounded current operations.
- `app/models/orders.py`, `app/models/reservation.py`, `app/models/revenue.py`: supporting index metadata.
- `alembic/versions/0054_manager_dashboard_indexes.py`: forward and reverse index migration.
- `tests/test_manager_dashboard.py`: SQLite/PostgreSQL calculation, permission, scope, performance and migration tests.
- `docs/manager-dashboard.md`: formulas, endpoint, deployment and limitations.

Frontend:

- `src/pages/manager/ManagerDashboardPage.jsx`: replace static widgets and lists, add filters and analytics.
- `src/components/manager/DashboardCharts.jsx`: responsive accessible bars and proportional donuts.
- `src/hooks/useManagerDashboard.js`: existing API/state/SSE integration and active-page refresh.
- `src/lib/managerDashboard.js`: business dates, formatting, chart helpers and request lifecycle.
- `src/assets/css/ManagerDashboardPage.css`: scoped responsive/theme-compatible extensions.
- `src/lib/managerDashboard.test.js`, `src/lib/managerDashboard.render.test.js`: lifecycle and component validation.
- `tests/fixtures/manager-dashboard.json`: test-only backend-generated response.
- `package.json`: dashboard test command.
