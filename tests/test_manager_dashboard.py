import asyncio
import json
import importlib.util
import os
import unittest
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.models.dining import DiningTable, RestaurantFloor
from app.models.orders import FoodOrder
from app.models.reservation import Reservation, ReservationTable
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.revenue import PlatformCommission
from app.models.reviews import Review
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction
from app.routers.manager.manager_dashboard import router
from app.schemas.manager_dashboard import DashboardQuery
from app.services.manager_dashboard import dashboard

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 8, tzinfo=UTC)


class DashboardFixtures:
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.fixtures()

    def fixtures(self):
        self.manager = self.user("Manager", UserRole.MANAGER)
        self.other_manager = self.user("Other Manager", UserRole.MANAGER)
        self.customer = self.user("Customer")
        self.new_customer = self.user("New Customer")
        self.other_customer = self.user("Other Customer")
        self.restaurant = Restaurant(manager_id=self.manager.id, name="Actual restaurant", address="Street", city="Pune", status=RestaurantStatus.APPROVED)
        self.other_restaurant = Restaurant(manager_id=self.other_manager.id, name="Private restaurant", address="Other", city="Delhi", status=RestaurantStatus.APPROVED)
        self.db.add_all([self.restaurant, self.other_restaurant])
        self.db.flush()

    def user(self, name, role=UserRole.CUSTOMER):
        row = User(name=name, email=f"{uuid.uuid4().hex}@example.test", password_hash="x", role=role, email_verified=True)
        self.db.add(row)
        self.db.flush()
        return row

    def order(self, *, at=NOW, total="100", status="COMPLETED", payment="PAID", customer=None, restaurant=None, items=None, **kwargs):
        row = FoodOrder(restaurant_id=(restaurant or self.restaurant).id, customer_id=(customer or self.customer).id,
            items=items or [], subtotal=Decimal(total), total=Decimal(total), status=status, payment_status=payment,
            payment_method="WALLET", created_at=at, **kwargs)
        self.db.add(row)
        self.db.flush()
        return row

    def visit(self, *, day=date(2026, 11, 13), status="CONFIRMED", customer=None, restaurant=None, **kwargs):
        row = Reservation(restaurant_id=(restaurant or self.restaurant).id, user_id=(customer or self.customer).id,
            reservation_date=day, start_time=time(13), end_time=time(14), number_of_guests=4, status=status,
            fee_amount=100, created_at=datetime(2026, 10, 1, tzinfo=UTC), **kwargs)
        self.db.add(row)
        self.db.flush()
        return row

    def collection(self, order=None, *, at=NOW, gross="100", commission="10", refunded_at=None, source="FOOD_ORDER", source_id=None, restaurant=None):
        restaurant = restaurant or self.restaurant
        row = PlatformCommission(manager_id=restaurant.manager_id, restaurant_id=restaurant.id, source_type=source,
            source_id=source_id or (order.id if order else uuid.uuid4()), gross_amount=Decimal(gross), commission_amount=Decimal(commission),
            commission_percent=10, occurred_at=at, refunded_at=refunded_at)
        self.db.add(row)
        self.db.flush()
        return row

    def result(self, start="2026-11-01", end="2026-11-13", **kwargs):
        return dashboard(self.db, self.restaurant, self.manager, DashboardQuery(start_date=start, end_date=end, **kwargs), now=NOW)



class DashboardCalculations(DashboardFixtures):
    def test_empty_data_has_zero_buckets_and_safe_comparisons(self):
        result = self.result()
        self.assertEqual(result.overview.revenue.value, 0)
        self.assertEqual(result.overview.revenue.change_percent, 0)
        self.assertEqual(len(result.revenue_chart), 13)
        self.assertTrue(all(p.revenue == 0 for p in result.revenue_chart))
        self.assertEqual(result.customers.retention_percent, 0)
        self.assertEqual(result.popular_items, [])
        self.assertEqual(result.live.tables.occupancy_percent, 0)

    def test_financial_cash_flow_refund_date_commission_and_wallet_exclusion(self):
        old = datetime(2026, 10, 25, 9, tzinfo=UTC)
        self.collection(at=NOW, gross="1000", commission="100")
        self.collection(at=NOW, gross="200", commission="20", refunded_at=NOW)
        self.collection(at=old, gross="300", commission="30", refunded_at=NOW)
        self.collection(at=NOW, gross="100", commission="10", refunded_at=datetime(2026, 11, 20, tzinfo=UTC))
        # A different restaurant and unrelated wallet balance/credits must not count.
        self.collection(restaurant=self.other_restaurant, gross="90000", commission="9000")
        self.db.add(WalletAccount(user_id=self.manager.id, balance=99999))
        self.db.add(WalletTransaction(user_id=self.manager.id, amount=99999, transaction_type="CREDIT", description="Top up", reference="topup"))
        self.order(total="5000", payment="FAILED")
        self.db.flush()
        result = self.result()
        self.assertEqual(result.revenue.gross, Decimal("1300.00"))
        self.assertEqual(result.revenue.refunds, Decimal("500.00"))
        self.assertEqual(result.revenue.revenue, Decimal("800.00"))
        self.assertEqual(result.revenue.commissions, Decimal("80.00"))
        self.assertEqual(result.revenue.net, Decimal("720.00"))
        self.assertEqual(result.overview.revenue.previous, Decimal("300.00"))
        self.assertEqual(result.overview.revenue.change_percent, Decimal("166.67"))
        self.assertEqual(sum(p.net for p in result.revenue_chart), result.revenue.net)

    def test_paid_reservation_preorder_is_not_counted_twice_after_materialization(self):
        visit = self.visit(payment_method="WALLET", preorder_payment_method="WALLET", preorder_total=400)
        order = self.order(total="400", reservation_id=visit.id)
        self.collection(source="RESERVATION_FEE", source_id=visit.id, gross="100")
        self.collection(source="PREORDER", source_id=visit.id, gross="400", commission="40")
        result = self.result()
        self.assertEqual(result.revenue.gross, 500)
        self.assertEqual(sum(m.count for m in result.payment_methods), 2)
        self.assertEqual(sum(m.amount for m in result.payment_methods), 500)

    def test_timezone_boundaries_and_equal_previous_period(self):
        # Restaurant-local 1 Nov midnight is 31 Oct 18:30 UTC.
        for at in (datetime(2026, 10, 31, 18, 29, 59, tzinfo=UTC), datetime(2026, 10, 31, 18, 30, tzinfo=UTC),
                   datetime(2026, 11, 1, 18, 29, 59, tzinfo=UTC), datetime(2026, 11, 1, 18, 30, tzinfo=UTC)):
            self.collection(self.order(at=at), at=at)
        result = self.result(start="2026-11-01", end="2026-11-01")
        self.assertEqual(result.overview.orders.value, 2)
        self.assertEqual(result.overview.orders.previous, 1)
        self.assertEqual(result.overview.orders.change_percent, 100)
        self.assertEqual(result.revenue.gross, 200)
        self.assertEqual(result.period.previous_start_date, date(2026, 10, 31))

    def test_status_trends_and_reservations_use_scheduled_dates(self):
        for status in ("PLACED", "ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED", "CANCELLED", "REJECTED"):
            self.order(status=status)
        for status in ("PENDING_PAYMENT", "CONFIRMED", "SEATED", "COMPLETED", "CANCELLED", "REJECTED", "NO_SHOW"):
            self.visit(status=status)
        self.visit(day=date(2026, 12, 1))
        self.order(restaurant=self.other_restaurant)
        result = self.result()
        self.assertEqual(result.overview.orders.value, 8)
        self.assertEqual(result.overview.reservations.value, 7)
        self.assertEqual(result.operations.order_completion_percent, Decimal("12.50"))
        self.assertEqual(result.operations.expected_guests, 12)
        self.assertEqual(result.operations.average_party_size, 4)
        self.assertEqual(result.operations.order_trend[-1].pending, 5)
        self.assertEqual(result.operations.reservation_trend[-1].cancelled, 3)
        self.assertEqual(result.operations.peak_reservation_days[0].label, "Friday")

    def test_customers_use_full_restaurant_history_and_unique_union(self):
        self.order(at=datetime(2026, 1, 1, tzinfo=UTC))
        self.order()
        self.visit()
        self.order(customer=self.new_customer)
        self.visit(customer=self.new_customer)
        self.order(restaurant=self.other_restaurant, customer=self.other_customer)
        self.order(customer=self.other_customer, status="CANCELLED")
        guest = self.order()
        guest.customer_id = None
        self.db.flush()
        result = self.result()
        self.assertEqual(result.overview.customers.value, 2)
        self.assertEqual(result.customers.new, 1)
        self.assertEqual(result.customers.returning, 1)
        self.assertEqual(result.customers.retention_percent, 50)
        self.assertEqual(result.customers.trend[-1].new, 1)
        self.assertEqual(result.customers.trend[-1].returning, 1)

    def test_popular_rankings_saved_discounts_refunds_and_aov(self):
        first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
        line = lambda id, name, quantity, total: {"variant_id": id, "food_name": name, "variant_name": "Regular", "quantity": quantity, "line_total": total}
        first = self.order(total="200", items=[line(first_id, "Tea", 10, 200)])
        first.discount_amount = 20
        first.total = 180
        second = self.order(total="500", items=[line(second_id, "Meal", 1, 500)])
        self.order(payment="REFUNDED", items=[line(first_id, "Tea", 50, 5000)])
        self.order(status="CANCELLED", items=[line(first_id, "Tea", 50, 5000)])
        self.order(payment="PENDING", items=[line(first_id, "Tea", 50, 5000)])
        self.order(restaurant=self.other_restaurant, items=[line(first_id, "Tea", 50, 5000)])
        self.db.flush()
        result = self.result()
        self.assertEqual([p.quantity for p in result.popular_items], [10, 1])
        self.assertEqual(result.popular_items[0].revenue, 180)
        self.assertEqual(result.overview.average_order_value.value, 340)
        self.assertEqual(self.result(popular_sort="revenue").popular_items[0].name, "Meal · Regular")
        self.assertIsNone(result.overview.average_order_value.change_percent)

    def test_item_renames_keep_one_rank_and_reversed_collections_do_not_qualify(self):
        variant = str(uuid.uuid4())
        line = lambda name: [{"variant_id": variant, "food_name": name, "variant_name": "Regular", "quantity": 1, "line_total": 100}]
        self.order(items=line("Old name"))
        self.order(items=line("New name"))
        reversed_order = self.order(items=line("Refunded name"), total="1000")
        self.collection(reversed_order, gross="1000", refunded_at=NOW)
        result = self.result()
        self.assertEqual(len(result.popular_items), 1)
        self.assertEqual(result.popular_items[0].quantity, 2)
        self.assertEqual(result.overview.average_order_value.value, 100)

    def test_new_indexes_can_upgrade_and_downgrade(self):
        from unittest.mock import patch
        # Migration directories are not Python packages in this repository.
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("dashboard_indexes", Path(__file__).parents[1] / "alembic/versions/0054_manager_dashboard_indexes.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        connection = self.db.connection()
        schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration.downgrade()
            migration.upgrade()
            for name, table, _ in migration.INDEXES:
                self.assertIn(name, {index["name"] for index in inspect(connection).get_indexes(table, schema=schema)})

    def test_visible_review_moderation_and_privacy(self):
        self.db.add_all([
            Review(customer_id=self.customer.id, restaurant_id=self.restaurant.id, rating=5, content="Public review", status="PUBLISHED", created_at=NOW),
            Review(customer_id=self.new_customer.id, restaurant_id=self.restaurant.id, rating=1, content="Hidden review", status="HIDDEN", created_at=NOW),
            Review(customer_id=self.other_customer.id, restaurant_id=self.other_restaurant.id, rating=1, content="Other review", status="PUBLISHED", created_at=NOW),
        ])
        self.db.flush()
        result = self.result()
        self.assertEqual(result.reviews.average, 5)
        self.assertEqual(result.reviews.total, 1)
        self.assertEqual(result.reviews.recent[0].content, "Public review")
        payload = result.model_dump_json()
        for private in (self.customer.email, "password_hash", "Hidden review", "Other review"):
            self.assertNotIn(private, payload)

    def test_live_tables_and_lists_do_not_follow_historical_filter(self):
        floor = RestaurantFloor(restaurant_id=self.restaurant.id, floor_number=1, floor_name="Main")
        inactive = RestaurantFloor(restaurant_id=self.restaurant.id, floor_number=2, floor_name="Closed", is_active=False)
        self.db.add_all([floor, inactive]); self.db.flush()
        def table(number, status="available", at=NOW, on_floor=None):
            row = DiningTable(restaurant_id=self.restaurant.id, floor_id=(on_floor or floor).id, table_number=number, status=status, updated_at=at)
            self.db.add(row); self.db.flush()
            return row
        seated_table = table("1")
        stale = table("2", "occupied", NOW - timedelta(days=2))
        table("3", "maintenance")
        table("4", on_floor=inactive)
        visit = self.visit(status="SEATED", checked_in_at=NOW)
        self.db.add(ReservationTable(reservation_id=visit.id, table_id=seated_table.id))
        upcoming = self.visit(day=date(2026, 11, 14))
        active = self.order(status="PREPARING")
        self.db.flush()
        result = self.result(start="2026-01-01", end="2026-01-02")
        self.assertEqual(result.overview.orders.value, 0)
        self.assertEqual(result.live.tables.total, 3)
        self.assertEqual(result.live.tables.occupied, 1)
        self.assertEqual(result.live.tables.available, 1)
        self.assertEqual(result.live.tables.occupancy_percent, Decimal("33.33"))
        self.assertEqual(result.live.upcoming_reservations[0].id, str(upcoming.id))
        self.assertEqual(result.live.active_orders[0].id, str(active.id))

    def test_all_date_groupings_are_zero_filled_and_bounded(self):
        self.order(at=datetime(2026, 11, 2, tzinfo=UTC))
        for group, expected in (("day", 13), ("week", 3), ("month", 1), ("year", 1)):
            result = self.result(group_by=group)
            self.assertEqual(len(result.revenue_chart), expected)
            self.assertEqual(sum(p.total for p in result.operations.order_trend), 1)
        self.assertEqual(self.result(start="2020-01-01", end="2026-11-13").period.group_by, "year")

    def test_query_count_does_not_grow_with_transaction_count(self):
        counts = []
        def count(*args):
            counts.append(1)
        event.listen(self.engine, "before_cursor_execute", count)
        try:
            self.result()
            baseline = len(counts)
            for index in range(30):
                self.order()
            counts.clear()
            self.result()
            self.assertEqual(len(counts), baseline)
            self.assertLessEqual(baseline, 25)
        finally:
            event.remove(self.engine, "before_cursor_execute", count)


class ManagerDashboardTests(DashboardCalculations, unittest.TestCase):
    pass


@unittest.skipUnless(os.getenv("DASHBOARD_TEST_POSTGRES") == "1", "Opt-in PostgreSQL rollback integration fixtures")
class PostgresManagerDashboardTests(DashboardCalculations, unittest.TestCase):
    def setUp(self):
        from app.db.database import engine
        self.engine = engine
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        schema = f"dashboard_test_{uuid.uuid4().hex}"
        self.connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        self.connection = self.connection.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(self.connection)
        self.db = Session(self.connection, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.fixtures()


class DashboardAPITests(DashboardFixtures, unittest.TestCase):
    # Reuse only setup/helpers, not all calculation tests.
    def request(self, query="start_date=2026-11-01&end_date=2026-11-13", user="manager", token=None):
        api = FastAPI()
        api.include_router(router)
        api.dependency_overrides[get_db] = lambda: self.db
        if token is None and user:
            token = create_access_token(str(getattr(self, user).id))[0]
        headers = [(b"cookie", f"dinebook_access_token={token}".encode())] if token else []
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "path": "/api/manager/dashboard/overview", "query_string": query.encode(), "headers": headers}
        async def run():
            await asyncio.wait_for(api(scope, receive, send), timeout=5)
        asyncio.run(run())
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return status, json.loads(body)

    def test_authentication_role_and_scoping(self):
        self.collection(gross="100")
        self.collection(gross="99999", restaurant=self.other_restaurant)
        self.db.commit()
        status, payload = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(Decimal(payload["revenue"]["gross"]), 100)
        self.assertEqual(self.request(user="customer")[0], 401)
        self.assertEqual(self.request(user=None)[0], 401)
        self.assertEqual(self.request(user=None, token="bad-token")[0], 401)
        self.assertEqual(self.request(query=f"restaurant_id={self.other_restaurant.id}")[0], 422)

    def test_suspended_unverified_inactive_and_unlinked_manager(self):
        self.restaurant.status = RestaurantStatus.SUSPENDED
        self.db.flush()
        self.assertEqual(self.request()[0], 403)
        self.restaurant.status = RestaurantStatus.APPROVED
        self.manager.email_verified = False
        self.db.flush()
        self.assertEqual(self.request()[0], 401)
        self.manager.email_verified = True
        self.manager.is_active = False
        self.db.flush()
        self.assertEqual(self.request()[0], 401)
        self.manager.is_active = True
        self.restaurant.manager_id = self.customer.id
        self.db.flush()
        self.assertEqual(self.request()[0], 404)

    def test_api_invalid_date_ranges_and_enums(self):
        for query in ("start_date=wrong&end_date=2026-11-01", "start_date=2026-11-13&end_date=2026-11-01",
                      "start_date=2026-11-01", "start_date=2026-02-30&end_date=2026-11-13", "group_by=invalid",
                      "start_date=2000-01-01&end_date=2026-11-01", "start_date=2024-01-01&end_date=2026-11-01&group_by=day"):
            with self.subTest(query=query):
                self.assertEqual(self.request(query=query)[0], 422)
        self.assertEqual(self.request(query="")[0], 200)
