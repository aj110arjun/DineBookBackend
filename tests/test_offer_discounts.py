import unittest
from tests import sqlite_support
import uuid
from datetime import date, time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.database import Base
from app.models.menu import Category, Food, FoodVariant
from app.models.orders import FoodOrderOffer, OfferRedemption
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.routers.commerce.orders import calculate_offer_discount, offer_data
from app.core.config import settings
from app.routers.commerce.orders import (CheckoutInput, CustomerOrderCancellation, OrderLineInput,
    cancel_customer_order, create_reserved_order, validate_customer_offer, preview_customer_offer,
    OfferValidationInput, OfferPreviewInput, customer_router)


class OfferDiscountTests(unittest.TestCase):
    def test_percentage_discount_is_capped_and_rounded(self):
        offer = SimpleNamespace(discount_type="PERCENTAGE", discount_percent=Decimal("20"), max_discount=Decimal("15.00"))
        self.assertEqual(calculate_offer_discount(offer, Decimal("100")), Decimal("15.00"))
        self.assertEqual(calculate_offer_discount(offer, Decimal("25.05")), Decimal("5.01"))

    def test_fixed_discount_cannot_exceed_eligible_subtotal(self):
        offer = SimpleNamespace(discount_type="FIXED", discount_percent=Decimal("80"), max_discount=None)
        self.assertEqual(calculate_offer_discount(offer, Decimal("20.00")), Decimal("20.00"))

    def test_offer_state_derives_exhaustion_without_mutating_offer(self):
        from datetime import datetime, timezone
        offer = SimpleNamespace(id="id", title="Meal deal", description=None, code="DEAL", discount_type="FIXED",
            discount_percent=Decimal("5"), max_discount=None, minimum_subtotal=Decimal("0"), uses=1, max_uses=1,
            per_customer_limit=None, applicability="RESTAURANT", eligible_food_ids=[], eligible_category_ids=[],
            is_active=True, starts_at=None, ends_at=None, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        self.assertEqual(offer_data(offer)["status"], "EXHAUSTED")


class OfferCheckoutTests(unittest.TestCase):
    def setUp(self):
        tax_setting = patch.object(settings, "order_tax_percent", Decimal("18.00"))
        tax_setting.start()
        self.addCleanup(tax_setting.stop)
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        manager = User(name="Manager", email="offer-manager@example.test", password_hash="x", role=UserRole.MANAGER)
        self.customer = User(name="Customer", email="offer-customer@example.test", password_hash="x", role=UserRole.CUSTOMER)
        self.db.add_all([manager, self.customer]); self.db.flush()
        self.restaurant = Restaurant(manager_id=manager.id, name="Restaurant", address="Street", city="Town", status=RestaurantStatus.APPROVED)
        self.db.add(self.restaurant); self.db.flush()
        category = Category(restaurant_id=self.restaurant.id, name="Mains")
        self.db.add(category); self.db.flush()
        self.food = Food(restaurant_id=self.restaurant.id, category_id=category.id, name="Pasta")
        side = Food(restaurant_id=self.restaurant.id, category_id=category.id, name="Side")
        self.db.add_all([self.food, side]); self.db.flush()
        self.variant = FoodVariant(food_id=self.food.id, name="Regular", price=Decimal("350.00"))
        self.side_variant = FoodVariant(food_id=side.id, name="Small", price=Decimal("200.00"))
        self.db.add_all([self.variant, self.side_variant]); self.db.flush()
        self.reservation = Reservation(user_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_date=date(2030, 1, 2), start_time=time(18), end_time=time(19, 30),
            number_of_guests=2, status="SEATED", fee_amount=Decimal("250.00"),
            payment_status="PAID", payment_method="WALLET")
        self.db.add(self.reservation); self.db.commit()

    def tearDown(self):
        self.db.close(); Base.metadata.drop_all(self.engine); self.engine.dispose()

    def payload(self, offer_code):
        return CheckoutInput(idempotency_key=uuid.uuid4(), reservation_id=self.reservation.id,
            offer_code=offer_code, items=[OrderLineInput(variant_id=self.variant.id, quantity=1),
                OrderLineInput(variant_id=self.side_variant.id, quantity=1)])

    def test_item_scoped_offer_only_discounts_matching_items(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="ITEM50", title="Pasta deal",
            discount_type="FIXED", discount_percent=Decimal("50.00"), minimum_subtotal=Decimal("300.00"),
            applicability="ITEMS", eligible_food_ids=[str(self.food.id)], max_uses=1, per_customer_limit=1)
        self.db.add(offer); self.db.commit()
        order = create_reserved_order(self.reservation.id, self.payload("item50"), self.customer, self.db)
        self.assertEqual((order["subtotal"], order["discount_amount"], order["total"]), (550.0, 50.0, 590.0))
        self.assertEqual((order["tax_percent"], order["tax_amount"]), (18.0, 90.0))
        self.assertEqual(self.db.query(OfferRedemption).one().discount_amount, Decimal("50.00"))

    def test_redemption_limit_is_released_after_cancellation(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="SAVE50", title="Half price",
            discount_type="FIXED", discount_percent=Decimal("50.00"), minimum_subtotal=Decimal("300.00"),
            applicability="RESTAURANT", max_uses=1, per_customer_limit=1)
        self.db.add(offer); self.db.commit()
        placed = create_reserved_order(self.reservation.id, self.payload("SAVE50"), self.customer, self.db)
        self.assertEqual((placed["discount_amount"], placed["total"]), (50.0, 590.0))
        with self.assertRaises(HTTPException):
            create_reserved_order(self.reservation.id, self.payload("SAVE50"), self.customer, self.db)
        cancelled = cancel_customer_order(uuid.UUID(placed["id"]), CustomerOrderCancellation(reason="Changed mind"), self.customer, self.db)
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self.db.query(OfferRedemption).one().status, "REVERSED")
        self.assertEqual(self.db.query(FoodOrderOffer).one().uses, 0)

    def offer_request(self, code, *, preview=False, items=None):
        data = {"offer_code": code, "items": items or [OrderLineInput(variant_id=self.variant.id, quantity=1)]}
        if preview:
            return OfferPreviewInput(restaurant_id=self.restaurant.id, **data)
        return OfferValidationInput(reservation_id=self.reservation.id, **data)

    def test_validation_rejects_duplicate_cart_lines(self):
        items = [OrderLineInput(variant_id=self.variant.id, quantity=1)] * 2
        with self.assertRaises(HTTPException) as caught:
            validate_customer_offer(self.offer_request("SAVE", items=items), self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 422)

    def test_offer_does_not_validate_when_no_cart_items_match(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="SIDE", title="Side deal",
            discount_type="FIXED", discount_percent=10, applicability="ITEMS",
            eligible_food_ids=[str(self.side_variant.food_id)])
        self.db.add(offer); self.db.commit()
        result = validate_customer_offer(self.offer_request("SIDE"), self.customer, self.db)
        self.assertFalse(result["valid"])
        self.assertEqual(result["discount_amount"], 0)

    def test_validation_allows_reuse_after_redemption_reversal(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="AGAIN", title="Reusable",
            discount_type="FIXED", discount_percent=10, per_customer_limit=1)
        self.db.add(offer); self.db.commit()
        placed = create_reserved_order(self.reservation.id, self.payload("AGAIN"), self.customer, self.db)
        cancel_customer_order(uuid.UUID(placed["id"]), CustomerOrderCancellation(reason="Changed mind"), self.customer, self.db)
        self.assertTrue(validate_customer_offer(self.offer_request("AGAIN"), self.customer, self.db)["valid"])

    def test_deleted_variants_are_rejected_by_both_preview_routes(self):
        self.variant.deleted_at = datetime.now(timezone.utc)
        self.db.commit()
        for preview, endpoint in ((False, validate_customer_offer), (True, preview_customer_offer)):
            with self.subTest(preview=preview), self.assertRaises(HTTPException) as caught:
                endpoint(self.offer_request("SAVE", preview=preview), self.customer, self.db)
            self.assertEqual(caught.exception.status_code, 409)

    def test_pending_preorder_claim_counts_toward_customer_limit(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="HELD", title="Limited",
            discount_type="FIXED", discount_percent=10, per_customer_limit=1)
        self.db.add(offer); self.db.flush()
        self.reservation.preorder_offer_id = offer.id
        self.db.commit()
        for preview, endpoint in ((False, validate_customer_offer), (True, preview_customer_offer)):
            self.assertFalse(endpoint(self.offer_request("HELD", preview=preview), self.customer, self.db)["valid"])
        with self.assertRaises(HTTPException) as caught:
            create_reserved_order(self.reservation.id, self.payload("HELD"), self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 422)

    def test_offer_preview_route_is_registered_once(self):
        matches = [route for route in customer_router.routes if route.path == "/api/customer/orders/offers/preview" and "POST" in route.methods]
        self.assertEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
