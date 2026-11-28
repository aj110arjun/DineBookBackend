import asyncio
import json
import os
import unittest
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import FastAPI, HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.realtime import RealtimeEvent
from app.models.reservation import ReservationTable
from app.models.user import AccountStatus, UserRole
from app.routers.chef.chef_dashboard import router
from app.routers.commerce.orders import CHEF_TRANSITIONS, chef_router, update_status
from app.schemas.chef_dashboard import ChefDashboardQuery
from app.services.chef_dashboard import dashboard
from tests.test_manager_dashboard import DashboardFixtures, NOW

UTC = timezone.utc


class ChefFixtures(DashboardFixtures):
    def fixtures(self):
        super().fixtures()
        self.chef = self.user("Kitchen Chef", UserRole.CHEF)
        self.chef.manager_id = self.manager.id
        self.other_chef = self.user("Other Chef", UserRole.CHEF)
        self.other_chef.manager_id = self.other_manager.id
        self.db.flush()

    def result(self, **kwargs):
        return dashboard(self.db, self.restaurant, ChefDashboardQuery(**kwargs), now=NOW)

    def visit(self, **kwargs):
        start_time, end_time = kwargs.pop("start_time", time(13)), kwargs.pop("end_time", time(14))
        row = super().visit(**kwargs)
        row.start_time, row.end_time = start_time, end_time
        self.db.flush()
        return row

    def table(self, number="T1", restaurant=None):
        restaurant = restaurant or self.restaurant
        floor = RestaurantFloor(restaurant_id=restaurant.id, floor_number=1, floor_name="Terrace")
        self.db.add(floor)
        self.db.flush()
        row = DiningTable(restaurant_id=restaurant.id, floor_id=floor.id, table_number=number)
        self.db.add(row)
        self.db.flush()
        return row


class ChefCalculations(ChefFixtures):
    def test_empty_dataset_has_actual_zero_counts_and_unknown_durations(self):
        result = self.result()
        self.assertEqual(result.summary.today_orders, 0)
        self.assertIsNone(result.summary.orders_change_percent)
        self.assertIsNone(result.summary.average_preparation_minutes)
        self.assertEqual(len(result.order_volume), 24)
        self.assertTrue(all(row.total == 0 for row in result.order_volume))
        self.assertEqual(len(result.preparation_performance), 7)
        self.assertTrue(all(row.average_minutes is None for row in result.preparation_performance))
        self.assertEqual(result.active_orders.total, 0)
        self.assertEqual(result.active_orders.items, [])
        self.assertEqual(result.upcoming_reservations, [])
        self.assertEqual(result.recent_activity, [])
        self.assertIsNone(result.performance.longest_active_seconds)
        self.assertIsNone(result.performance.delayed_orders)

    def test_daily_counts_statuses_and_all_age_live_queue(self):
        for status in ("PLACED", "ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED", "CANCELLED", "REJECTED"):
            self.order(status=status)
        self.order(status="ACCEPTED", at=NOW - timedelta(days=1))
        self.order(status="COMPLETED", restaurant=self.other_restaurant)
        result = self.result()
        self.assertEqual(result.summary.today_orders, 8)
        self.assertEqual(result.summary.yesterday_orders, 1)
        self.assertEqual(result.summary.orders_change_percent, 700)
        self.assertEqual(result.summary.pending_orders, 3)
        self.assertEqual(result.summary.preparing_orders, 1)
        self.assertEqual(result.summary.ready_orders, 1)
        self.assertEqual(result.summary.completed_orders, 1)
        self.assertEqual(result.active_orders.total, 5)
        self.assertEqual(sum(row.count for row in result.status_distribution), 8)
        self.assertEqual(sum(row.total for row in result.order_volume), 8)

    def test_local_midnight_hour_and_range_boundaries(self):
        boundary = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
        self.order(at=boundary - timedelta(microseconds=1))
        self.order(at=boundary)
        self.order(at=datetime(2026, 11, 13, 18, 29, 59, tzinfo=UTC))
        self.order(at=datetime(2026, 11, 13, 18, 30, tzinfo=UTC))
        result = self.result()
        self.assertEqual(result.today, date(2026, 11, 13))
        self.assertEqual(result.summary.today_orders, 2)
        self.assertEqual(result.summary.yesterday_orders, 1)
        self.assertEqual(result.order_volume[0].total, 1)
        self.assertEqual(result.order_volume[-1].total, 1)
        for period, length in (("last7", 7), ("last30", 30)):
            with self.subTest(period=period):
                ranged = self.result(period=period)
                self.assertEqual(len(ranged.order_volume), length)
                self.assertEqual(sum(point.total for point in ranged.order_volume), 3)
                self.assertEqual(ranged.group_by, "day")
                self.assertEqual(ranged.summary.today_orders, 2)

    def test_preparation_uses_ready_date_and_valid_timestamp_pairs_only(self):
        for minutes in (10, 20):
            self.order(at=NOW - timedelta(days=1), status="COMPLETED", completed_at=NOW,
                       preparation_started_at=NOW - timedelta(minutes=minutes), ready_at=NOW)
        self.order(status="READY", at=NOW - timedelta(hours=1), ready_at=NOW)
        self.order(status="READY", at=NOW - timedelta(hours=1),
                   preparation_started_at=NOW, ready_at=NOW - timedelta(minutes=1))
        self.order(status="CANCELLED", at=NOW - timedelta(hours=1),
                   preparation_started_at=NOW - timedelta(minutes=55), ready_at=NOW)
        self.order(status="READY", restaurant=self.other_restaurant, at=NOW - timedelta(hours=2),
                   preparation_started_at=NOW - timedelta(minutes=90), ready_at=NOW)
        result = self.result()
        self.assertAlmostEqual(result.summary.average_preparation_minutes, 15, places=3)
        self.assertEqual(result.performance.prepared_today, 4)
        self.assertEqual(result.performance.measured_today, 2)
        self.assertEqual(result.summary.completed_orders, 2)
        self.assertEqual(result.preparation_performance[-1].measured_orders, 2)
        self.assertIsNone(result.preparation_performance[0].average_minutes)

    def test_preparation_crosses_midnight_and_allows_real_zero_minutes(self):
        boundary = datetime(2026, 11, 12, 18, 30, tzinfo=UTC)
        self.order(status="SERVED", at=boundary - timedelta(hours=1),
                   preparation_started_at=boundary - timedelta(minutes=10), ready_at=boundary)
        self.order(status="READY", preparation_started_at=NOW, ready_at=NOW)
        result = self.result()
        self.assertAlmostEqual(result.summary.average_preparation_minutes, 5, places=3)
        self.assertEqual(result.performance.measured_today, 2)

    def test_oldest_first_pagination_status_filters_and_allowed_actions(self):
        first = self.order(status="PLACED", at=NOW - timedelta(days=2))
        second = self.order(status="ACCEPTED", at=NOW - timedelta(minutes=30))
        third = self.order(status="PREPARING", at=NOW - timedelta(minutes=20))
        self.order(status="READY")
        result = self.result(page_size=2, page=2)
        self.assertEqual(result.active_orders.total, 4)
        self.assertEqual(result.active_orders.items[0].id, str(third.id))
        self.assertEqual(result.active_orders.items[0].allowed_actions, ["READY"])
        self.assertEqual(self.result(page_size=2, page=99).active_orders.page, 2)
        filtered = self.result(status="ACCEPTED")
        self.assertEqual(filtered.active_orders.total, 1)
        self.assertEqual(filtered.active_orders.items[0].id, str(second.id))
        self.assertEqual(filtered.active_orders.items[0].age_rank, 2)
        self.assertEqual(filtered.active_orders.items[0].allowed_actions, ["PREPARING"])
        self.assertEqual(self.result().active_orders.items[0].allowed_actions, [])
        self.assertEqual(self.result().active_orders.items[0].id, str(first.id))
        self.assertEqual(self.result().performance.longest_active_seconds, 2 * 86400)
        self.assertIsNotNone(self.result().active_orders.items[0].created_at.utcoffset())

    def test_tables_preorders_arrival_sorting_and_no_personal_or_financial_fields(self):
        table = self.table()
        tomorrow = self.visit(day=date(2026, 11, 14), fulfillment_type="PREORDER",
            preorder_items=[{"food_name": "Garden salad", "variant_name": "Large", "quantity": 2, "unit_price": 999}])
        today = self.visit(start_time=time(14), end_time=time(15))
        self.visit(start_time=time(12))  # Already elapsed in restaurant time.
        self.visit(status="SEATED")
        self.visit(day=date(2026, 11, 14), restaurant=self.other_restaurant)
        self.db.add(ReservationTable(reservation_id=tomorrow.id, table_id=table.id))
        order = self.order(status="ACCEPTED", reservation_id=tomorrow.id, customer_note="No peanuts",
                           items=[{"food_name": "Garden salad", "quantity": 2, "unit_price": 999, "customer_email": "private"}])
        self.order(status="READY", table_id=table.id)
        self.db.flush()
        result = self.result()
        self.assertEqual([row.id for row in result.upcoming_reservations], [str(today.id), str(tomorrow.id)])
        self.assertEqual(result.upcoming_reservations[1].tables[0].floor_name, "Terrace")
        self.assertEqual(result.upcoming_reservations[1].preorder_items[0].quantity, 2)
        active = next(row for row in result.active_orders.items if row.id == str(order.id))
        self.assertEqual(active.item_count, 2)
        self.assertEqual(active.customer_note, "No peanuts")
        self.assertEqual(active.tables[0].table_number, "T1")
        payload = result.model_dump_json()
        for private in ("customer_email", "unit_price", "payment_status", "customer_name", self.customer.email, self.other_restaurant.name):
            self.assertNotIn(private, payload)

    def test_activity_is_persisted_scoped_bounded_and_does_not_invent_transitions(self):
        order = self.order(status="READY")
        for index in range(15):
            self.db.add(RealtimeEvent(restaurant_id=self.restaurant.id, event_type="order.status_changed", resource_id=str(order.id),
                data={"status": "READY", "previous_status": "PREPARING", "actor_id": "private"}, created_at=NOW - timedelta(seconds=index)))
        self.db.add(RealtimeEvent(restaurant_id=self.other_restaurant.id, event_type="order.status_changed", resource_id="private",
            data={"status": "READY", "previous_status": "PREPARING"}, created_at=NOW))
        self.db.add(RealtimeEvent(restaurant_id=self.restaurant.id, event_type="order.status_changed", resource_id=str(order.id),
            data={"status": "READY"}, created_at=NOW))  # Payment-only update, no status transition.
        self.db.flush()
        result = self.result()
        self.assertGreater(len(result.recent_activity), 0)
        self.assertLessEqual(len(result.recent_activity), 12)
        self.assertTrue(all(row.order_id == str(order.id) for row in result.recent_activity))
        self.assertTrue(all(row.description == "Order marked ready" for row in result.recent_activity))
        self.assertNotIn("actor_id", result.model_dump_json())

    def test_existing_transitions_record_timestamps_and_reject_repeats_and_cross_restaurant(self):
        order = self.order(status="ACCEPTED")
        self.db.commit()
        self.assertEqual(update_status(order.id, "PREPARING", CHEF_TRANSITIONS, self.chef, self.db)["status"], "PREPARING")
        with self.assertRaises(HTTPException) as repeated:
            update_status(order.id, "PREPARING", CHEF_TRANSITIONS, self.chef, self.db)
        self.assertEqual(repeated.exception.status_code, 409)
        with self.assertRaises(HTTPException) as cross_scope:
            update_status(order.id, "READY", CHEF_TRANSITIONS, self.other_chef, self.db)
        self.assertEqual(cross_scope.exception.status_code, 404)
        self.assertEqual(update_status(order.id, "READY", CHEF_TRANSITIONS, self.chef, self.db)["status"], "READY")
        self.assertIsNotNone(order.preparation_started_at)
        self.assertIsNotNone(order.ready_at)
        with self.assertRaises(HTTPException) as invalid:
            update_status(order.id, "COMPLETED", CHEF_TRANSITIONS, self.chef, self.db)
        self.assertEqual(invalid.exception.status_code, 409)

    def test_query_count_is_bounded_as_queue_grows(self):
        counts = []
        def count(*args):
            counts.append(1)
        event.listen(self.engine, "before_cursor_execute", count)
        try:
            for _ in range(2):
                self.order(status="ACCEPTED")
            counts.clear()
            self.result()
            baseline = len(counts)
            for _ in range(30):
                self.order(status="ACCEPTED")
            counts.clear()
            result = self.result()
            self.assertEqual(len(counts), baseline)
            self.assertLessEqual(baseline, 12)
            self.assertEqual(len(result.active_orders.items), 8)
        finally:
            event.remove(self.engine, "before_cursor_execute", count)


class ChefDashboardTests(ChefCalculations, unittest.TestCase):
    pass


@unittest.skipUnless(os.getenv("CHEF_DASHBOARD_TEST_POSTGRES") == "1", "Opt-in PostgreSQL rollback integration")
class PostgresChefDashboardTests(ChefCalculations, unittest.TestCase):
    def setUp(self):
        from app.db.database import engine
        self.engine = engine
        connection = engine.connect()
        transaction = connection.begin()
        self.addCleanup(connection.close)
        self.addCleanup(transaction.rollback)
        schema = f"chef_dashboard_test_{uuid.uuid4().hex}"
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection = connection.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(connection)
        self.db = Session(connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        self.addCleanup(self.db.close)
        self.fixtures()


class ChefDashboardAPITests(ChefFixtures, unittest.TestCase):
    def request(self, *, user="chef", query="", token=None, path="/api/chef/dashboard", method="GET", body=None):
        api = FastAPI()
        api.include_router(router)
        api.include_router(chef_router)
        api.dependency_overrides[get_db] = lambda: self.db
        if token is None and user:
            token = create_access_token(str(getattr(self, user).id))[0]
        headers = [(b"content-type", b"application/json")]
        if token:
            headers.append((b"cookie", f"dinebook_access_token={token}".encode()))
        messages = []
        async def receive():
            return {"type": "http.request", "body": json.dumps(body).encode() if body else b"", "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "path": path, "query_string": query.encode(), "headers": headers}
        async def run():
            await asyncio.wait_for(api(scope, receive, send), timeout=5)
        asyncio.run(run())
        status = next(row["status"] for row in messages if row["type"] == "http.response.start")
        content = b"".join(row.get("body", b"") for row in messages if row["type"] == "http.response.body")
        return status, json.loads(content)

    def test_chef_authentication_and_restaurant_isolation(self):
        allowed = self.order(status="ACCEPTED")
        private = self.order(status="PREPARING", restaurant=self.other_restaurant)
        self.db.commit()
        status, body = self.request(query=f"restaurant_id={self.other_restaurant.id}")
        self.assertEqual(status, 200)  # Arbitrary IDs are ignored; scope comes from the session.
        self.assertEqual(body["active_orders"]["items"][0]["id"], str(allowed.id))
        self.assertNotIn(str(private.id), json.dumps(body))
        for user in (None, "customer", "manager"):
            with self.subTest(user=user):
                self.assertEqual(self.request(user=user)[0], 401)
        self.assertEqual(self.request(token="invalid")[0], 401)

    def test_account_restrictions_and_missing_restaurant(self):
        self.chef.must_change_password = True
        self.db.commit()
        self.assertEqual(self.request()[0], 403)
        self.chef.must_change_password = False
        self.chef.status = AccountStatus.INACTIVE
        self.db.commit()
        self.assertEqual(self.request()[0], 401)
        self.chef.status = AccountStatus.ACTIVE
        self.chef.manager_id = None
        self.db.commit()
        self.assertEqual(self.request()[0], 404)

    def test_query_validation_and_unauthorized_actions(self):
        for query in ("period=all", "status=COMPLETED", "page=0", "page_size=51"):
            self.assertEqual(self.request(query=query)[0], 422)
        order = self.order(status="ACCEPTED")
        self.db.commit()
        path = f"/api/chef/orders/{order.id}/status"
        self.assertEqual(self.request(path=path, method="PATCH", body={"status": "PREPARING"})[0], 200)
        self.assertEqual(self.request(path=path, method="PATCH", body={"status": "PREPARING"})[0], 409)
        self.assertEqual(self.request(path=path, method="PATCH", body={"status": "READY"}, user="other_chef")[0], 404)
        self.assertEqual(self.request(path=path, method="PATCH", body={"status": "READY"}, user="manager")[0], 401)


if __name__ == "__main__":
    unittest.main()
