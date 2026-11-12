import asyncio
import json
import unittest
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests import sqlite_support
from app.db.database import Base, get_db
from app.models.menu import Category, Food, FoodVariant
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.routers.customer.customer_restaurants import router
from app.services.menu_listing import menu_page


class PreorderMenuTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        manager = User(name="Manager", email="menu@example.com", password_hash="x", role=UserRole.MANAGER)
        self.db.add(manager)
        self.db.flush()
        self.restaurant = Restaurant(manager_id=manager.id, name="Menu test", address="1 Main", city="Pune", status=RestaurantStatus.APPROVED)
        self.db.add(self.restaurant)
        self.db.flush()
        self.starters = self.category("Starters")
        self.mains = self.category("Mains", display_order=1)
        self.apple = self.food("Apple salad", self.starters, 150, vegetarian=True, description="Fresh and crunchy")
        self.biryani = self.food("Biryani", self.mains, 250)
        self.curry = self.food("Curry", self.mains, 200, vegetarian=True, variant_name="Family portion")
        self.db.commit()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = lambda: self.db

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def category(self, name, **kwargs):
        category = Category(restaurant_id=self.restaurant.id, name=name, **kwargs)
        self.db.add(category)
        self.db.flush()
        return category

    def food(self, name, category, price, *, vegetarian=False, variant_name="Regular", **kwargs):
        food = Food(restaurant_id=self.restaurant.id, category_id=category.id, name=name, is_vegetarian=vegetarian, **kwargs)
        self.db.add(food)
        self.db.flush()
        self.db.add(FoodVariant(food_id=food.id, name=variant_name, price=price))
        self.db.flush()
        return food

    def browse(self, **kwargs):
        return menu_page(self.db, self.restaurant.id, page=kwargs.pop("page", 1), page_size=kwargs.pop("page_size", 8), public=True, preorder=True, **kwargs)

    def get(self, query="", *, suffix="preorder-menu", restaurant_id=None):
        messages = []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "GET", "path": f"/api/customer/restaurants/{restaurant_id or self.restaurant.id}/{suffix}",
                 "query_string": query.encode(), "root_path": "", "scheme": "http",
                 "server": ("test", 80), "client": ("127.0.0.1", 1), "headers": []}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(body)

    def test_default_browses_all_categories(self):
        status, result = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 3)
        self.assertEqual([item["name"] for item in result["items"]], ["Apple salad", "Biryani", "Curry"])
        self.assertEqual((result["page"], result["page_size"]), (1, 8))

    def test_filters_are_combined_before_count_and_pagination(self):
        status, result = self.get(f"category_id={self.mains.id}&is_vegetarian=true&search=family&page_size=1")
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], str(self.curry.id))
        self.assertTrue(result["items"][0]["is_vegetarian"])

    def test_search_matches_name_description_category_and_variant(self):
        for search, expected in [(" SALAD ", ["Apple salad"]), ("CRUNCHY", ["Apple salad"]),
                                 ("mains", ["Biryani", "Curry"]), ("family", ["Curry"])]:
            with self.subTest(search=search):
                self.assertEqual([item["name"] for item in self.browse(search=search)["items"]], expected)

    def test_category_counts_reflect_search_and_diet_across_all_pages(self):
        result = self.browse(is_vegetarian=True, category_id=self.mains.id, page_size=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual({category["name"]: category["count"] for category in result["categories"]}, {"Starters": 1, "Mains": 1})
        self.assertEqual(self.browse(is_vegetarian=False)["items"][0]["id"], str(self.biryani.id))

    def test_name_and_price_sorting_is_global_before_pagination(self):
        for sort, expected in [("name", ["Apple salad", "Biryani", "Curry"]),
                               ("name-desc", ["Curry", "Biryani", "Apple salad"]),
                               ("price-low", ["Apple salad", "Curry", "Biryani"]),
                               ("price-high", ["Biryani", "Curry", "Apple salad"])]:
            with self.subTest(sort=sort):
                pages = [self.browse(sort=sort, page=page, page_size=1) for page in range(1, 4)]
                self.assertEqual([page["items"][0]["name"] for page in pages], expected)
                self.assertTrue(all(page["total"] == 3 for page in pages))

    def test_sql_applies_limit_and_offset(self):
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))

        event.listen(self.engine, "before_cursor_execute", capture)
        result = self.browse(page=2, page_size=1)
        event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(result["items"][0]["name"], "Biryani")
        self.assertTrue(any("LIMIT" in sql and "OFFSET" in sql and params[-2:] == (1, 1) for sql, params in statements))

    def test_only_orderable_items_and_variants_are_returned(self):
        now = datetime.now(timezone.utc)
        inactive = self.category("Inactive", is_active=False)
        deleted = self.category("Deleted", deleted_at=now)
        self.food("Inactive category food", inactive, 1)
        self.food("Deleted category food", deleted, 1)
        self.food("Unavailable food", self.mains, 1, is_available=False)
        self.food("Deleted food", self.mains, 1, deleted_at=now)
        unavailable = self.food("Unavailable option", self.mains, 1)
        unavailable.variants[0].is_available = False
        removed = self.food("Removed option", self.mains, 1)
        removed.variants[0].deleted_at = now
        self.db.add(Food(restaurant_id=self.restaurant.id, category_id=self.mains.id, name="No options"))
        self.db.add_all([
            FoodVariant(food_id=self.biryani.id, name="Unavailable cheap", price=1, is_available=False),
            FoodVariant(food_id=self.biryani.id, name="Deleted cheap", price=1, deleted_at=now),
        ])
        self.db.commit()
        result = self.browse(sort="price-low")
        self.assertEqual(result["total"], 3)
        self.assertEqual([item["name"] for item in result["items"]], ["Apple salad", "Curry", "Biryani"])
        self.assertEqual([variant["name"] for variant in result["items"][-1]["variants"]], ["Regular"])
        self.assertEqual(len(result["categories"]), 2)
        self.assertEqual(self.browse(search="Unavailable cheap")["total"], 0)

    def test_multiple_variants_do_not_duplicate_foods_or_counts(self):
        self.db.add(FoodVariant(food_id=self.curry.id, name="Small", price=50))
        self.db.commit()
        result = self.browse(sort="price-low")
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["items"][0]["name"], "Curry")
        self.assertEqual(len(result["items"][0]["variants"]), 2)

    def test_tied_prices_have_stable_pages(self):
        for food in (self.apple, self.biryani, self.curry):
            food.variants[0].price = 100
        self.db.commit()
        ids = [self.browse(sort="price-low", page=page, page_size=1)["items"][0]["id"] for page in range(1, 4)]
        self.assertEqual(ids, sorted([str(self.apple.id), str(self.biryani.id), str(self.curry.id)]))

    def test_empty_search_and_out_of_range_page(self):
        self.assertEqual(self.browse(search="missing")["total"], 0)
        result = self.browse(page=100)
        self.assertEqual((result["items"], result["total"]), ([], 3))
        self.assertEqual(self.browse(category_id=uuid.uuid4())["total"], 0)

    def test_other_restaurants_are_excluded_and_hidden_restaurants_rejected(self):
        manager = User(name="Other", email="other-menu@example.com", password_hash="x", role=UserRole.MANAGER)
        self.db.add(manager)
        self.db.flush()
        other = Restaurant(manager_id=manager.id, name="Other", address="2 Main", city="Pune", status=RestaurantStatus.APPROVED)
        self.db.add(other)
        self.db.flush()
        category = Category(restaurant_id=other.id, name="Other category")
        self.db.add(category)
        self.db.flush()
        self.db.add(Food(restaurant_id=other.id, category_id=category.id, name="Other food"))
        self.db.commit()
        self.assertEqual(self.browse(category_id=category.id)["total"], 0)
        for status in (RestaurantStatus.PENDING, RestaurantStatus.SUSPENDED):
            self.restaurant.status = status
            self.db.commit()
            with self.assertRaises(HTTPException) as caught:
                self.browse()
            self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.get(restaurant_id=uuid.uuid4())[0], 404)

    def test_http_rejects_invalid_query_parameters(self):
        for query in ("page=0", "page_size=0", "page_size=101", "sort=invalid", "category_id=invalid", "is_vegetarian=invalid", "search=" + "x" * 101):
            with self.subTest(query=query):
                self.assertEqual(self.get(query)[0], 422)

    def test_existing_menu_contracts_still_work(self):
        status, legacy = self.get(suffix="menu")
        self.assertEqual(status, 200)
        self.assertIsInstance(legacy, list)
        self.assertEqual(len(legacy), 2)
        status, paged = self.get("page=1", suffix="menu")
        self.assertEqual(status, 200)
        self.assertEqual(paged["total"], 1)
        self.assertEqual(paged["items"][0]["name"], "Apple salad")


if __name__ == "__main__":
    unittest.main()
