"""Regression tests for floor layouts and multi-table selection."""
import unittest
from datetime import date, time
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests import sqlite_support
from app.db.database import Base
from app.models.dining import DiningTable, RestaurantFloor
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.models.reservation import Reservation, ReservationTable
from app.routers.restaurants.dining import TableCreate, TableUpdate
from app.routers.restaurants.reservations import floor_layout_data, suitable_table_groups
from fastapi import BackgroundTasks


class TableFloorLayoutTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        manager = User(name="Manager", email="layout@example.com", password_hash="unused", role=UserRole.MANAGER)
        self.db.add(manager)
        self.db.flush()
        self.restaurant = Restaurant(manager_id=manager.id, name="Layout", address="Street", city="Pune", status=RestaurantStatus.APPROVED)
        self.db.add(self.restaurant)
        self.db.flush()
        self.floor = RestaurantFloor(restaurant_id=self.restaurant.id, floor_number=1, floor_name="Ground")
        self.db.add(self.floor)
        self.db.flush()
        self.tables = [DiningTable(restaurant_id=self.restaurant.id, floor_id=self.floor.id,
            table_number=f"T-{i + 1}", capacity=2, reservation_fee=250,
            grid_row=0, grid_col=i, status="available") for i in range(3)]
        self.db.add_all(self.tables)
        self.db.commit()
        self.day = date(2035, 1, 2)
        self.manager_id = manager.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_reserved_tables_remain_visible_with_slot_specific_status(self):
        booking = Reservation(user_id=self.manager_id, restaurant_id=self.restaurant.id,
            reservation_date=self.day, start_time=time(18), end_time=time(19, 30),
            number_of_guests=2, fee_amount=250, status="CONFIRMED")
        self.db.add(booking)
        self.db.flush()
        self.db.add(ReservationTable(reservation_id=booking.id, table_id=self.tables[1].id))
        self.db.commit()
        layout = floor_layout_data(self.db, self.restaurant.id, self.day, time(18))
        self.assertEqual([t["grid_col"] for t in layout], [0, 1, 2])
        self.assertEqual([t["status"] for t in layout], ["available", "reserved", "available"])
        later = floor_layout_data(self.db, self.restaurant.id, self.day, time(20))
        self.assertTrue(all(t["status"] == "available" for t in later))
        self.assertEqual({g[0].id for g in suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 4)}, {self.tables[0].id, self.tables[2].id})

    def test_tables_in_separate_positions_support_larger_parties(self):
        self.tables[1].grid_col = 20
        self.tables[2].grid_row = 30
        self.db.commit()
        groups = suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 6)
        self.assertEqual({g[0].id for g in groups}, {t.id for t in self.tables})

    def test_maintenance_is_visible_but_not_bookable(self):
        self.tables[1].status = "maintenance"
        self.db.commit()
        layout = floor_layout_data(self.db, self.restaurant.id, self.day, time(18))
        self.assertEqual(layout[1]["status"], "maintenance")
        self.assertEqual({g[0].id for g in suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 4)}, {self.tables[0].id, self.tables[2].id})

    def test_duplicate_positions_are_rejected(self):
        self.tables[1].grid_col = 0
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_manager_coordinates_are_required_and_validated(self):
        payload = dict(floor_id=self.floor.id, table_number="New", capacity=2)
        with self.assertRaises(ValidationError):
            TableCreate(**payload)
        self.assertEqual(TableCreate(**payload, grid_row=0, grid_col=4).grid_col, 4)
        for value in (-1, 101, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TableUpdate(grid_row=value)

    def test_legacy_tables_can_be_combined_using_the_displayed_default_layout(self):
        for table in self.tables:
            table.grid_row = None
            table.grid_col = None
        self.db.commit()
        layout = floor_layout_data(self.db, self.restaurant.id, self.day, time(18))
        by_id = {item["id"]: item for item in layout}
        for index, table in enumerate(self.tables):
            item = by_id[str(table.id)]
            self.assertEqual((item["grid_row"], item["grid_col"]), (0, index))
            self.assertEqual(item["layout_source"], "automatic")
        groups = suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 6)
        self.assertEqual({g[0].id for g in groups}, {t.id for t in self.tables})
        self.assertEqual(sum(t.reservation_fee for t in self.tables), 750)
        self.tables[1].status = "maintenance"
        self.db.commit()
        refreshed = floor_layout_data(self.db, self.restaurant.id, self.day, time(18))
        self.assertEqual({item["id"]: (item["grid_row"], item["grid_col"]) for item in refreshed},
                         {item["id"]: (item["grid_row"], item["grid_col"]) for item in layout})
        self.assertEqual(suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 6), [])

    def test_tables_on_different_floors_can_be_selected_together(self):
        other_floor = RestaurantFloor(restaurant_id=self.restaurant.id,
                                     floor_number=2, floor_name="Upstairs")
        self.db.add(other_floor)
        self.db.flush()
        self.tables[2].floor_id = other_floor.id
        self.db.commit()
        groups = suitable_table_groups(self.db, self.restaurant.id, self.day, time(18), 6)
        self.assertEqual({g[0].id for g in groups}, {t.id for t in self.tables})

    def test_time_slots_include_selectable_tables_and_blocked_tables(self):
        from unittest.mock import patch
        from app.routers.restaurants.reservations import reservation_time_slots
        self.tables[1].status = "maintenance"
        self.db.commit()
        with patch("app.routers.restaurants.reservations.effective_periods",
                   return_value=([{"opens": "18:00", "closes": "20:00"}], None)):
            result = reservation_time_slots(self.restaurant.id, self.day, 4, self.db)
        slot = result["slots"][0]
        self.assertTrue(slot["available"])
        self.assertEqual(len(slot["tables"]), 3)
        self.assertEqual({item["status"] for item in slot["tables"]}, {"available", "maintenance"})
        self.assertTrue(all(isinstance(item["grid_row"], int) for item in slot["tables"]))

    def test_booking_arbitrary_tables_charges_combined_price_and_reserves_both(self):
        from unittest.mock import patch
        from decimal import Decimal
        from app.models.wallet import WalletAccount, WalletTransaction
        from app.routers.restaurants.reservations import ReservationCreate, create_reservation
        customer = User(name="Customer", email="booking@example.com", password_hash="unused", role=UserRole.CUSTOMER)
        self.db.add(customer)
        self.db.flush()
        self.db.add(User(name="Admin", email="settlement@example.com",
                         password_hash="unused", role=UserRole.ADMIN))
        wallet = WalletAccount(user_id=customer.id, balance=1000)
        self.db.add(wallet)
        self.db.commit()
        payload = ReservationCreate(restaurant_id=self.restaurant.id,
            reservation_date=self.day, start_time=time(18), number_of_guests=4,
            table_ids=[self.tables[0].id, self.tables[2].id], payment_method="WALLET",
            duration_minutes=180)
        with patch("app.routers.restaurants.reservations.within_operating_hours", return_value=True), \
             patch("app.routers.restaurants.reservations.send_reservation_confirmation"):
            result = create_reservation(payload, BackgroundTasks(), customer=customer, db=self.db)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["end_time"], "21:00")
        self.assertEqual(result["fee_amount"], 500)
        self.db.refresh(wallet)
        self.assertEqual(wallet.balance, Decimal("500.00"))
        self.assertEqual(self.db.query(ReservationTable).count(), 2)
        transactions = self.db.query(WalletTransaction).filter_by(user_id=customer.id).all()
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].amount, Decimal("-500.00"))

    def test_booking_insufficient_capacity_returns_specific_error(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        from app.routers.restaurants.reservations import ReservationCreate, create_reservation
        payload = ReservationCreate(restaurant_id=self.restaurant.id,
            reservation_date=self.day, start_time=time(18), number_of_guests=7,
            table_ids=[self.tables[0].id, self.tables[2].id])
        customer = self.db.get(User, self.manager_id)
        with patch("app.routers.restaurants.reservations.within_operating_hours", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                create_reservation(payload, BackgroundTasks(), customer=customer, db=self.db)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("3 more seats needed", caught.exception.detail)
        self.assertEqual(self.db.query(Reservation).count(), 0)

    def test_longer_period_checks_conflicts_after_the_default_ninety_minutes(self):
        booking = Reservation(user_id=self.manager_id, restaurant_id=self.restaurant.id,
            reservation_date=self.day, start_time=time(20), end_time=time(21),
            number_of_guests=2, fee_amount=250, status="CONFIRMED")
        self.db.add(booking)
        self.db.flush()
        self.db.add(ReservationTable(reservation_id=booking.id, table_id=self.tables[0].id))
        self.db.commit()
        short = floor_layout_data(self.db, self.restaurant.id, self.day, time(18), 90)
        long = floor_layout_data(self.db, self.restaurant.id, self.day, time(18), 180)
        self.assertEqual(short[0]["status"], "available")
        self.assertEqual(long[0]["status"], "reserved")
        self.assertEqual(suitable_table_groups(self.db, self.restaurant.id,
            self.day, time(18), 6, duration_minutes=180), [])

    def test_extended_period_must_fit_before_closing(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        from app.routers.restaurants.reservations import (
            reservation_time_slots, ReservationCreate, create_reservation,
        )
        periods = ([{"opens": "18:00", "closes": "22:00"}], None)
        with patch("app.routers.restaurants.reservations.effective_periods", return_value=periods):
            slots = reservation_time_slots(self.restaurant.id, self.day, 2, self.db, 180)["slots"]
            self.assertEqual([slot["time"] for slot in slots], ["18:00", "18:30", "19:00"])
            self.assertEqual(slots[-1]["end_time"], "22:00")
            payload = ReservationCreate(restaurant_id=self.restaurant.id,
                reservation_date=self.day, start_time=time(20), number_of_guests=2,
                table_ids=[self.tables[0].id], duration_minutes=180)
            with self.assertRaises(HTTPException) as caught:
                create_reservation(payload, BackgroundTasks(), customer=self.db.get(User, self.manager_id), db=self.db)
            self.assertEqual(caught.exception.status_code, 409)

    def test_duration_bounds_and_legacy_default(self):
        from app.routers.restaurants.reservations import ReservationCreate
        payload = dict(restaurant_id=self.restaurant.id, reservation_date=self.day,
                       start_time=time(18), number_of_guests=2)
        self.assertEqual(ReservationCreate(**payload).duration_minutes, 90)
        for duration in (0, 29, 721, 120.5):
            with self.subTest(duration=duration), self.assertRaises(ValidationError):
                ReservationCreate(**payload, duration_minutes=duration)

    def test_admin_inspection_uses_selected_duration(self):
        from unittest.mock import patch
        from app.routers.restaurants.operating_hours import admin_availability_inspection
        with patch("app.routers.restaurants.operating_hours.effective_periods",
                   return_value=([{"opens": "18:00", "closes": "22:00"}], None)):
            result = admin_availability_inspection(self.restaurant.id, self.day,
                time(18), 2, self.db.get(User, self.manager_id), self.db, 180)
        self.assertEqual(result["end_time"], "21:00")
        self.assertTrue(result["available"])
