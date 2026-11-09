import hashlib
import hmac
import importlib.util
import os
import unittest
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.dining import DiningTable, RestaurantFloor
from app.models.reservation import Reservation
from app.models.revenue import PlatformCommission
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction
from app.routers.restaurants.dining import TableUpdate, update_table
from app.routers.restaurants.reservations import (
    RESTAURANT_TIMEZONE,
    ReservationCreate,
    ReservationPaymentVerification,
    availability,
    cancel_reservation,
    create_reservation,
    get_my_reservation,
    verify_reservation_payment,
)
from app.services.reservation_invoice_pdf import build_reservation_invoice_pdf
from tests import test_offer_discounts as fixtures


class ReservationPricingTests(unittest.TestCase):
    def setUp(self):
        fixtures.OfferCheckoutTests.setUp(self)
        self.addCleanup(fixtures.OfferCheckoutTests.tearDown, self)
        hours = patch("app.routers.restaurants.reservations.within_operating_hours", return_value=True)
        hours.start()
        self.addCleanup(hours.stop)
        self.manager = self.db.get(User, self.restaurant.manager_id)
        self.day = (datetime.now(RESTAURANT_TIMEZONE) + timedelta(days=2)).date()
        self.start = time(12)
        self.floor = RestaurantFloor(restaurant_id=self.restaurant.id, floor_name="Main", floor_number=1)
        self.db.add(self.floor)
        self.db.flush()
        self.table = self.add_table("A1", "250.00", "100.25")
        self.db.add_all([
            User(name="Admin", email="pricing-admin@example.test", password_hash="x", role=UserRole.ADMIN),
            WalletAccount(user_id=self.customer.id, balance=Decimal("5000.00")),
        ])
        self.db.commit()

    def add_table(self, number, base, rate):
        row = DiningTable(restaurant_id=self.restaurant.id, floor_id=self.floor.id,
                          table_number=number, capacity=2, reservation_fee=Decimal(base),
                          extra_hour_fee=Decimal(rate))
        self.db.add(row)
        self.db.flush()
        return row

    def quote(self, minutes):
        result = availability(self.restaurant.id, self.day, self.start, 2, self.db, minutes)
        return next(table for table in result["tables"] if table["id"] == str(self.table.id))

    def book(self, minutes, **options):
        fields = dict(restaurant_id=self.restaurant.id, reservation_date=self.day, start_time=self.start,
                      duration_minutes=minutes, number_of_guests=2, table_ids=[self.table.id])
        fields.update(options)
        return create_reservation(ReservationCreate(**fields), BackgroundTasks(), self.customer, self.db)

    def test_quote_charges_each_started_hour_after_first_hour(self):
        for minutes, hours, fee in ((30, 0, 250), (60, 0, 250), (61, 1, 350.25), (90, 1, 350.25),
                                    (120, 1, 350.25), (121, 2, 450.50), (720, 11, 1352.75)):
            with self.subTest(minutes=minutes):
                quote = self.quote(minutes)
                self.assertEqual(quote["base_fee_amount"], 250)
                self.assertEqual(quote["extra_hours"], hours)
                self.assertEqual(quote["fee_amount"], fee)
                self.assertEqual(quote["extra_fee_amount"], fee - 250)

    def test_manager_rate_is_validated_and_changes_future_quotes(self):
        for invalid in (None, "-1.00", "100000.01", "1.001", "NaN", "Infinity"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                TableUpdate(extra_hour_fee=invalid)
        saved = update_table(self.table.id, TableUpdate(extra_hour_fee="80.10"), self.manager, self.db)
        self.assertEqual(saved["extra_hour_fee"], 80.10)
        self.assertEqual(self.quote(121)["fee_amount"], 410.20)
        update_table(self.table.id, TableUpdate(extra_hour_fee=0), self.manager, self.db)
        self.assertEqual(self.quote(121)["fee_amount"], 250)

    def test_rate_change_rejects_stale_checkout_without_charging_wallet(self):
        original_quote = self.quote(90)
        update_table(self.table.id, TableUpdate(extra_hour_fee="150.00"), self.manager, self.db)
        with self.assertRaises(HTTPException) as caught:
            self.book(90, expected_fee_amount=original_quote["fee_amount"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.get(WalletAccount, self.customer.id).balance, Decimal("5000.00"))
        self.assertEqual(self.db.query(WalletTransaction).count(), 0)
        self.assertEqual(self.db.query(Reservation).count(), 1)  # Only the original fixture.

    def test_wallet_debit_and_refund_use_booked_multitable_price(self):
        other = self.add_table("A2", "200.00", "75.50")
        self.db.commit()
        booked = self.book(121, table_ids=[self.table.id, other.id], number_of_guests=4,
                           expected_fee_amount="801.50")
        booking_id = uuid.UUID(booked["id"])
        self.assertEqual(booked["base_fee_amount"], 450)
        self.assertEqual(booked["extra_hour_rate"], 175.75)
        self.assertEqual(booked["extra_hours"], 2)
        self.assertEqual(booked["extra_fee_amount"], 351.50)
        self.assertEqual(booked["fee_amount"], 801.50)
        self.assertEqual(self.db.get(WalletAccount, self.customer.id).balance, Decimal("4198.50"))
        commission = self.db.query(PlatformCommission).filter_by(source_id=booking_id).one()
        self.assertEqual(commission.gross_amount, Decimal("801.50"))

        update_table(self.table.id, TableUpdate(reservation_fee=999, extra_hour_fee=999), self.manager, self.db)
        stored = get_my_reservation(booking_id, self.customer, self.db)
        self.assertEqual(stored["fee_amount"], 801.50)
        self.assertEqual(stored["extra_hour_rate"], 175.75)
        item = self.db.get(Reservation, booking_id)
        pdf = build_reservation_invoice_pdf(
            item, self.restaurant, self.customer, [self.table, other], "RES-TEST",
            Decimal(0), Decimal(0), Decimal(0), item.fee_amount, Decimal(0), False,
        )
        self.assertIn(b"Additional dining time", pdf)
        self.assertIn(b"INR 175.75", pdf)
        self.assertNotIn(b"INR 999.00", pdf)
        refund = cancel_reservation(booking_id, self.customer, self.db)
        self.assertEqual(refund["wallet_refund"], 801.50)
        self.assertEqual(self.db.get(WalletAccount, self.customer.id).balance, Decimal("5000.00"))

    def test_razorpay_order_and_verification_use_saved_extended_fee(self):
        order_id = "order_pricing_001"
        payment_id = "pay_pricing_001"
        secret = "pricing-test-secret"
        path = "app.routers.restaurants.reservations.razorpay_request"
        with patch(path, return_value={"id": order_id, "amount": 35025, "currency": "INR"}) as provider:
            booked = self.book(90, payment_method="RAZORPAY", expected_fee_amount="350.25")
        self.assertEqual(provider.call_args.args[2]["amount"], 35025)
        self.assertEqual(booked["payment_checkout"]["amount"], 35025)
        self.assertEqual(booked["payment_status"], "PENDING")

        update_table(self.table.id, TableUpdate(extra_hour_fee=999), self.manager, self.db)
        signature = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        verification = ReservationPaymentVerification(razorpay_order_id=order_id, razorpay_payment_id=payment_id,
                                                       razorpay_signature=signature)
        capture = {"order_id": order_id, "amount": 35025, "currency": "INR", "status": "captured"}
        with patch.object(settings, "razorpay_key_secret", secret), patch(path, return_value=capture):
            paid = verify_reservation_payment(uuid.UUID(booked["id"]), verification, BackgroundTasks(),
                                              self.customer, self.db)
        self.assertEqual(paid["payment_status"], "PAID")
        self.assertEqual(paid["fee_amount"], 350.25)
        self.assertEqual(paid["extra_hour_rate"], 100.25)


@unittest.skipUnless(os.environ.get("DINEBOOK_TEST_POSTGRES") == "1", "Requires local PostgreSQL")
class ReservationPricingMigrationTests(unittest.TestCase):
    def test_migration_keeps_existing_fees_and_defaults_rates_to_zero(self):
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0049_reservation_extra_hours.py"
        spec = importlib.util.spec_from_file_location("reservation_pricing_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = create_engine(settings.database_url)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("CREATE TEMP TABLE dining_tables (id INTEGER) ON COMMIT DROP"))
            connection.execute(text("CREATE TEMP TABLE reservations (fee_amount NUMERIC(10, 2)) ON COMMIT DROP"))
            connection.execute(text("INSERT INTO dining_tables VALUES (1)"))
            connection.execute(text("INSERT INTO reservations VALUES (250.25)"))
            ops = Operations(MigrationContext.configure(connection))
            with patch.object(migration, "op", ops):
                migration.upgrade()
                row = connection.execute(text(
                    "SELECT fee_amount, base_fee_amount, extra_hour_rate, extra_hours, extra_fee_amount FROM reservations"
                )).one()
                self.assertEqual(row, (Decimal("250.25"), Decimal("250.25"), Decimal(0), 0, Decimal(0)))
                self.assertEqual(connection.scalar(text("SELECT extra_hour_fee FROM dining_tables")), Decimal(0))
                with self.assertRaises(IntegrityError), connection.begin_nested():
                    connection.execute(text("UPDATE dining_tables SET extra_hour_fee = -1"))
                migration.downgrade()
            self.assertEqual(connection.scalar(text("SELECT fee_amount FROM reservations")), Decimal("250.25"))
