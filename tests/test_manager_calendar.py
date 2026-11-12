import unittest
from datetime import date, datetime, time, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests import sqlite_support
from app.db.database import Base
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.routers.restaurants.reservations import manager_calendar_summary


class ManagerCalendarTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.customer = User(name="Customer", email="calendar@example.test", password_hash="x", role=UserRole.CUSTOMER)
        self.db.add(self.customer)
        self.restaurants = []
        for index in range(2):
            manager = User(name="Manager", email=f"calendar-manager{index}@example.test", password_hash="x", role=UserRole.MANAGER)
            self.db.add(manager)
            self.db.flush()
            restaurant = Restaurant(manager_id=manager.id, name="Calendar test", address="Street", city="Mumbai", status=RestaurantStatus.APPROVED)
            self.db.add(restaurant)
            self.db.flush()
            self.restaurants.append(restaurant)
        self.restaurant = self.restaurants[0]
        for status in ("CONFIRMED", "SEATED", "PENDING_PAYMENT", "CANCELLED", "COMPLETED"):
            reservation = Reservation(
                user_id=self.customer.id, restaurant_id=self.restaurant.id,
                reservation_date=date(2030, 1, 2), start_time=time(18), end_time=time(19, 30),
                number_of_guests=2, status=status, fee_amount=250,
            )
            self.db.add(reservation)
            self.db.flush()
            self.db.add(FoodOrder(
                customer_id=self.customer.id, restaurant_id=self.restaurant.id,
                reservation_id=reservation.id, items=[], subtotal=10, total=10,
                status="PLACED", created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_manager_calendar_matches_date_keys_and_combines_status_markings(self):
        manager = self.db.get(User, self.restaurant.manager_id)
        result = manager_calendar_summary(date(2030, 1, 1), date(2030, 1, 2), manager, self.db)

        self.assertEqual(set(result["days"]), {"2030-01-02"})
        day = result["days"]["2030-01-02"]
        self.assertEqual(day["reservations_count"], 5)
        self.assertEqual(day["orders_count"], 5)
        self.assertEqual(day["guest_count"], 10)
        self.assertCountEqual(day["tones"], ["confirmed", "pending", "cancelled", "completed"])

    def test_manager_calendar_places_standalone_orders_on_local_date(self):
        for restaurant, created_at in (
            (self.restaurant, datetime(2030, 1, 2, 20, tzinfo=timezone.utc)),
            (self.restaurant, datetime(2030, 1, 2, 18, 29, 59, tzinfo=timezone.utc)),
            (self.restaurant, datetime(2030, 1, 3, 18, 30, tzinfo=timezone.utc)),
            (self.restaurants[-1], datetime(2030, 1, 2, 20, tzinfo=timezone.utc)),
        ):
            self.db.add(FoodOrder(
                customer_id=self.customer.id, restaurant_id=restaurant.id,
                items=[], subtotal=10, total=10, status="PLACED", created_at=created_at,
            ))
        self.db.commit()
        manager = self.db.get(User, self.restaurant.manager_id)
        result = manager_calendar_summary(date(2030, 1, 3), date(2030, 1, 3), manager, self.db)

        self.assertEqual(result["days"], {
            "2030-01-03": {
                "reservations_count": 0, "orders_count": 1,
                "guest_count": 0, "tones": ["pending"],
            },
        })

