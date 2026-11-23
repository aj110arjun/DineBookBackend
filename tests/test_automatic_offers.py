import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError
from app.models.offers import FoodOrderOffer, OfferRedemption
from app.models.menu import Category, Food, FoodVariant
from app.models.user import User
from app.routers.commerce.orders import (
    OfferInput, OfferPreviewInput, SpotOrderInput, SpotOrderPreviewInput,
    create_reserved_order, preview_customer_offer, manager_create_spot_order, manager_preview_spot_order,
    create_offer,
    update_offer, toggle_offer, reverse_offer_redemption, customer_offer_uses, queue_reservation_preorder,
)
from app.routers.restaurants.reservations import PreorderItem
from tests import test_offer_discounts as fixtures
from tests import test_reservation_pricing as reservation_fixtures


class AutomaticOfferTests(unittest.TestCase):
    setUp = fixtures.OfferCheckoutTests.setUp
    tearDown = fixtures.OfferCheckoutTests.tearDown
    payload = fixtures.OfferCheckoutTests.payload

    def offer(self, **options):
        fields = dict(restaurant_id=self.restaurant.id, code=f"OFFER-{uuid.uuid4().hex[:8]}", title="Menu offer",
                      discount_type="PERCENTAGE", discount_percent=10, application_mode="AUTOMATIC")
        fields.update(options)
        row = FoodOrderOffer(**fields)
        self.db.add(row)
        self.db.commit()
        return row

    def preview(self, code=None):
        return preview_customer_offer(OfferPreviewInput(restaurant_id=self.restaurant.id, offer_code=code,
            items=self.payload(None).items), self.customer, self.db)

    def test_automatic_discount_uses_scope_and_best_saving_before_tax(self):
        self.offer(discount_percent=5)
        winner = self.offer(applicability="ITEMS", eligible_food_ids=[str(self.food.id)], discount_percent=20)
        quote = self.preview()
        self.assertEqual(quote["offer"]["id"], str(winner.id))
        self.assertEqual((quote["eligible_subtotal"], quote["discount_amount"], quote["tax_amount"], quote["total"]),
                         (550, 80, 84.6, 554.6))
        self.assertEqual(winner.uses, 0)
        self.assertEqual(self.db.query(OfferRedemption).count(), 0)

    def test_category_offer_does_not_discount_other_categories(self):
        category = Category(restaurant_id=self.restaurant.id, name="Drinks")
        self.db.add(category)
        self.db.flush()
        self.side_variant.food.category_id = category.id
        self.db.commit()
        self.offer(applicability="CATEGORIES", eligible_category_ids=[str(self.food.category_id)], discount_percent=20)
        self.assertEqual(self.preview()["discount_amount"], 70)

    def test_dates_inactive_and_exhausted_offers_are_skipped(self):
        now = datetime.now(timezone.utc)
        for fields in [dict(starts_at=now + timedelta(days=1)), dict(ends_at=now - timedelta(seconds=1)),
                       dict(is_active=False), dict(max_uses=1, uses=1)]:
            self.offer(discount_percent=90, **fields)
        eligible = self.offer(discount_percent=10)
        self.assertEqual(self.preview()["offer"]["id"], str(eligible.id))

    def test_coupon_is_explicit_and_replaces_automatic_offer(self):
        automatic = self.offer(discount_percent=10)
        coupon = self.offer(application_mode="COUPON", code="SAVE20", discount_percent=20)
        self.assertEqual(self.preview()["offer"]["id"], str(automatic.id))
        quote = self.preview(" save20 ")
        self.assertEqual((quote["offer"]["application_mode"], quote["discount_amount"]), ("COUPON", 110))
        created = create_reserved_order(self.reservation.id, self.payload(coupon.code), self.customer, self.db)
        self.assertEqual(created["offer_code"], "SAVE20")
        self.assertEqual(created["discount_amount"], 110)
        self.assertEqual(automatic.uses, 0)

    def test_automatic_checkout_claims_once_and_retry_keeps_original_price(self):
        offer = self.offer(max_uses=1)
        payload = self.payload(None)
        created = create_reserved_order(self.reservation.id, payload, self.customer, self.db)
        self.assertEqual((created["offer_code"], created["discount_amount"]), (offer.code, 55))
        self.assertEqual(create_reserved_order(self.reservation.id, payload, self.customer, self.db)["id"], created["id"])
        self.assertEqual(offer.uses, 1)
        self.assertEqual(self.db.query(OfferRedemption).count(), 1)
        self.assertFalse(self.preview()["valid"])

    def test_minimum_cap_and_customer_limits_are_checked_on_eligible_items(self):
        self.offer(discount_percent=90, minimum_subtotal=400, applicability="ITEMS", eligible_food_ids=[str(self.food.id)])
        limited = self.offer(discount_percent=80, per_customer_limit=1)
        self.reservation.preorder_offer_id = limited.id
        self.db.commit()
        winner = self.offer(discount_percent=30, max_discount=20)
        result = self.preview()
        self.assertEqual((result["offer"]["id"], result["discount_amount"]), (str(winner.id), 20))

    def test_walkin_automatically_applies_offer_and_respects_account_requirements(self):
        self.offer(discount_percent=90, per_customer_limit=1)
        winner = self.offer(discount_percent=10)
        manager = self.db.get(User, self.restaurant.manager_id)
        quote = manager_preview_spot_order(SpotOrderPreviewInput(items=self.payload(None).items), manager, self.db)
        self.assertEqual((quote["offer_code"], quote["discount_amount"]), (winner.code, 55))
        request = SpotOrderInput(service_type="TAKEAWAY", guest_name="Guest", idempotency_key=uuid.uuid4(), items=self.payload(None).items)
        created = manager_create_spot_order(request, manager, self.db)
        self.assertEqual((created["discount_amount"], created["tax_amount"], created["total"]), (55, 89.1, 584.1))
        self.assertEqual(manager_create_spot_order(request, manager, self.db)["id"], created["id"])
        self.assertEqual(winner.uses, 1)

    def test_no_eligible_offer_returns_undiscounted_quote(self):
        self.offer(application_mode="COUPON")
        result = self.preview()
        self.assertEqual((result["valid"], result["offer"], result["discount_amount"], result["total"]), (False, None, 0, 649))

    def test_coupon_requires_a_code_but_automatic_offer_does_not(self):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        fields = dict(title="Menu deal", discount_percent=10, starts_at=start, ends_at=start + timedelta(days=1))
        self.assertEqual(OfferInput(**fields).application_mode, "AUTOMATIC")
        manager = self.db.get(User, self.restaurant.manager_id)
        automatic = create_offer(OfferInput(**fields), manager, self.db)
        self.assertTrue(automatic["code"].startswith("AUTO-"))
        self.assertEqual(automatic["application_mode"], "AUTOMATIC")
        coupon = create_offer(OfferInput(**fields, application_mode="COUPON", code="SAVE10"), manager, self.db)
        self.assertEqual((coupon["code"], coupon["application_mode"]), ("SAVE10", "COUPON"))
        with self.assertRaises(ValidationError):
            OfferInput(**fields, application_mode="COUPON")

    def test_changed_discount_blocks_stale_checkout_without_consuming_an_offer(self):
        offer = self.offer(discount_percent=10)
        payload = self.payload(None).model_copy(update={"expected_total": Decimal("550.00")})
        with self.assertRaises(HTTPException) as error:
            create_reserved_order(self.reservation.id, payload, self.customer, self.db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.db.get(FoodOrderOffer, offer.id).uses, 0)
        self.assertEqual(self.db.query(OfferRedemption).count(), 0)

    def test_category_and_item_offers_pick_one_discount_per_dish_and_redeem_once(self):
        self.variant.price = self.side_variant.price = Decimal("100.00")
        food = Food(restaurant_id=self.restaurant.id, category_id=self.food.category_id, name="Third dish")
        self.db.add(food)
        self.db.flush()
        third = FoodVariant(food_id=food.id, name="Regular", price=100)
        self.db.add(third)
        self.db.commit()
        category = self.offer(discount_percent=20, applicability="CATEGORIES", eligible_category_ids=[str(self.food.category_id)])
        item = self.offer(discount_percent=30, applicability="ITEMS", eligible_food_ids=[str(self.food.id)])
        payload = self.payload(None)
        payload.items.append(type(payload.items[0])(variant_id=third.id, quantity=1))
        quote = preview_customer_offer(OfferPreviewInput(restaurant_id=self.restaurant.id, items=payload.items), self.customer, self.db)
        self.assertEqual((quote["subtotal"], quote["discount_amount"], quote["total"]), (300, 70, 271.4))
        self.assertEqual(quote["item_discounts"][str(self.variant.id)]["discount_amount"], 30)
        self.assertEqual(quote["item_discounts"][str(self.side_variant.id)]["discount_amount"], 20)
        self.assertEqual(quote["item_discounts"][str(third.id)]["discount_amount"], 20)
        created = create_reserved_order(self.reservation.id, payload, self.customer, self.db)
        self.assertEqual(created["discount_amount"], 70)
        self.assertEqual(len(created["applied_offers"]), 2)
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)
        self.assertEqual((item.uses, category.uses), (1, 1))
        self.assertEqual(create_reserved_order(self.reservation.id, payload, self.customer, self.db)["id"], created["id"])
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)
        from app.models.orders import FoodOrder
        reverse_offer_redemption(self.db, self.db.get(FoodOrder, uuid.UUID(created["id"])))
        self.assertEqual((item.uses, category.uses), (0, 0))
        self.assertTrue(all(row.status == "REVERSED" for row in self.db.query(OfferRedemption)))

    def test_higher_category_offer_beats_lower_item_offer_without_stacking(self):
        category = self.offer(discount_percent=30, applicability="CATEGORIES", eligible_category_ids=[str(self.food.category_id)])
        item = self.offer(discount_percent=20, applicability="ITEMS", eligible_food_ids=[str(self.food.id)])
        quote = self.preview()
        self.assertEqual(quote["discount_amount"], 165)
        self.assertEqual(quote["item_discounts"][str(self.variant.id)]["applied_offer"]["id"], str(category.id))
        self.assertEqual(len(quote["applied_offers"]), 1)
        self.assertEqual(item.uses, 0)

    def test_walkin_preserves_each_winning_offer_and_retry_does_not_redeem_again(self):
        category = self.offer(discount_percent=20, applicability="CATEGORIES", eligible_category_ids=[str(self.food.category_id)])
        item = self.offer(discount_percent=30, applicability="ITEMS", eligible_food_ids=[str(self.food.id)])
        manager = self.db.get(User, self.restaurant.manager_id)
        request = SpotOrderInput(service_type="TAKEAWAY", guest_name="Guest", idempotency_key=uuid.uuid4(), items=self.payload(None).items)
        quote = manager_preview_spot_order(request, manager, self.db)
        self.assertEqual((quote["discount_amount"], quote["tax_amount"], quote["total"]), (145, 72.9, 477.9))
        self.assertEqual(quote["item_discounts"][str(self.variant.id)]["discount_amount"], 105)
        self.assertEqual(quote["item_discounts"][str(self.side_variant.id)]["discount_amount"], 40)
        created = manager_create_spot_order(request.model_copy(update={"expected_total": Decimal("477.90")}), manager, self.db)
        self.assertEqual(created["total"], quote["total"])
        self.assertEqual({row["id"] for row in created["applied_offers"]}, {str(category.id), str(item.id)})
        self.assertEqual(sum(row.get("discount_amount", 0) for row in created["items"]), 145)
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)
        self.assertEqual(manager_create_spot_order(request, manager, self.db)["id"], created["id"])
        self.assertEqual((category.uses, item.uses), (1, 1))
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)

    def test_fixed_value_and_discount_cap_are_not_duplicated_across_items(self):
        self.offer(discount_type="FIXED", discount_percent=80)
        quote = self.preview()
        self.assertEqual(quote["discount_amount"], 80)
        self.assertEqual(sum(row["discount_amount"] for row in quote["item_discounts"].values()), 80)
        for row in self.db.query(FoodOrderOffer):
            row.is_active = False
        self.db.commit()
        self.offer(discount_percent=80, max_discount=50)
        self.assertEqual(self.preview()["discount_amount"], 50)


class AutomaticPreorderTests(unittest.TestCase):
    setUp = reservation_fixtures.ReservationPricingTests.setUp
    add_table = reservation_fixtures.ReservationPricingTests.add_table
    book = reservation_fixtures.ReservationPricingTests.book

    def test_preorder_is_discounted_automatically_without_discounting_table_fee(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code="AUTO10", title="Preorder offer",
                              discount_percent=10, application_mode="AUTOMATIC")
        self.db.add(offer)
        self.db.commit()
        created = self.book(60, fulfillment_type="PREORDER", preorder_payment_method="PAY_AT_DESK",
                            preorder_items=[PreorderItem(variant_id=self.variant.id, quantity=1)])
        self.assertEqual((created["fee_amount"], created["preorder_discount"], created["preorder_total"]), (250, 35, 371.7))
        self.assertEqual(created["preorder_offer_code"], "AUTO10")
        self.assertEqual(offer.uses, 1)

    def make_multi_offer_booking(self):
        category = FoodOrderOffer(restaurant_id=self.restaurant.id, code="CATEGORY20", title="Category offer", discount_percent=20,
                                 applicability="CATEGORIES", eligible_category_ids=[str(self.food.category_id)])
        item = FoodOrderOffer(restaurant_id=self.restaurant.id, code="ITEM30", title="Item offer", discount_percent=30,
                             applicability="ITEMS", eligible_food_ids=[str(self.food.id)])
        self.db.add_all([category, item])
        self.db.commit()
        booking = self.book(60, fulfillment_type="PREORDER", preorder_payment_method="PAY_AT_DESK", preorder_items=[
            PreorderItem(variant_id=self.variant.id, quantity=1), PreorderItem(variant_id=self.side_variant.id, quantity=1)])
        return category, item, booking

    def test_preorder_holds_and_releases_all_winning_offers(self):
        category, item, booking = self.make_multi_offer_booking()
        self.assertEqual(booking["preorder_discount"], 145)
        self.assertEqual(len(booking["preorder_applied_offers"]), 2)
        self.assertEqual(customer_offer_uses(self.db, item.id, self.customer.id), 1)
        self.assertEqual(customer_offer_uses(self.db, category.id, self.customer.id), 1)
        from app.routers.restaurants.reservations import cancel_reservation
        cancel_reservation(uuid.UUID(booking["id"]), self.customer, self.db)
        self.assertEqual((category.uses, item.uses), (0, 0))

    def test_kitchen_handoff_retains_prices_and_does_not_claim_offers_twice(self):
        category, item, booking = self.make_multi_offer_booking()
        from app.models.reservation import Reservation
        reservation = self.db.get(Reservation, uuid.UUID(booking["id"]))
        reservation.status = "SEATED"
        order = queue_reservation_preorder(self.db, reservation, self.manager)
        self.db.commit()
        self.assertEqual(order.discount_amount, 145)
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)
        self.assertEqual((category.uses, item.uses), (1, 1))
        self.assertEqual(customer_offer_uses(self.db, item.id, self.customer.id), 1)
        self.assertEqual(customer_offer_uses(self.db, category.id, self.customer.id), 1)
        self.assertEqual(queue_reservation_preorder(self.db, reservation, self.manager).id, order.id)
        self.assertEqual(self.db.query(OfferRedemption).count(), 2)


class CategoryOfferWindowTests(unittest.TestCase):
    setUp = fixtures.OfferCheckoutTests.setUp
    tearDown = fixtures.OfferCheckoutTests.tearDown

    def payload(self, code, start=0, end=2, **extra):
        first_day = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)
        fields = dict(code=code, title=code, discount_percent=20, applicability="CATEGORIES",
                      eligible_category_ids=[self.food.category_id], timezone_name="Asia/Kolkata",
                      starts_at=first_day + timedelta(days=start), ends_at=first_day + timedelta(days=end))
        fields.update(extra)
        return OfferInput(**fields)

    def manager(self):
        return self.db.get(User, self.restaurant.manager_id)

    def test_overlapping_dates_for_same_category_are_rejected(self):
        create_offer(self.payload("FIRST"), self.manager(), self.db)
        with self.assertRaises(HTTPException) as error:
            create_offer(self.payload("SECOND", start=1, end=3), self.manager(), self.db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Mains", error.exception.detail)
        self.assertIn("FIRST", error.exception.detail)
        self.assertEqual(self.db.query(FoodOrderOffer).count(), 1)

    def test_shared_end_day_overlaps_but_next_day_and_future_windows_are_allowed(self):
        create_offer(self.payload("FIRST"), self.manager(), self.db)
        with self.assertRaises(HTTPException):
            create_offer(self.payload("SHARED", start=2, end=3), self.manager(), self.db)
        create_offer(self.payload("NEXT", start=3, end=4), self.manager(), self.db)
        create_offer(self.payload("FUTURE", start=10, end=12), self.manager(), self.db)
        self.assertEqual(self.db.query(FoodOrderOffer).count(), 3)

    def test_item_restaurant_and_coupon_offers_can_overlap_category_offer(self):
        create_offer(self.payload("CATEGORY"), self.manager(), self.db)
        create_offer(self.payload("ITEM", applicability="ITEMS", eligible_category_ids=[], eligible_food_ids=[self.food.id]), self.manager(), self.db)
        create_offer(self.payload("RESTAURANT", applicability="RESTAURANT", eligible_category_ids=[]), self.manager(), self.db)
        create_offer(self.payload("COUPON", application_mode="COUPON"), self.manager(), self.db)
        self.assertEqual(self.db.query(FoodOrderOffer).count(), 4)

    def test_editing_own_window_is_allowed_but_editing_into_another_window_is_rejected(self):
        first = create_offer(self.payload("FIRST"), self.manager(), self.db)
        update_offer(uuid.UUID(first["id"]), self.payload("FIRST"), self.manager(), self.db)
        second = create_offer(self.payload("SECOND", start=4, end=5), self.manager(), self.db)
        with self.assertRaises(HTTPException):
            update_offer(uuid.UUID(second["id"]), self.payload("SECOND", start=1, end=4), self.manager(), self.db)

    def test_reactivating_an_overlapping_category_offer_is_rejected(self):
        first = create_offer(self.payload("FIRST"), self.manager(), self.db)
        toggle_offer(uuid.UUID(first["id"]), False, self.manager(), self.db)
        create_offer(self.payload("SECOND"), self.manager(), self.db)
        with self.assertRaises(HTTPException):
            toggle_offer(uuid.UUID(first["id"]), True, self.manager(), self.db)
        self.assertFalse(self.db.get(FoodOrderOffer, uuid.UUID(first["id"])).is_active)

    def test_offers_for_different_categories_may_share_dates(self):
        other = Category(restaurant_id=self.restaurant.id, name="Desserts")
        self.db.add(other)
        self.db.commit()
        create_offer(self.payload("MAINS"), self.manager(), self.db)
        create_offer(self.payload("DESSERTS", eligible_category_ids=[other.id]), self.manager(), self.db)
        self.assertEqual(self.db.query(FoodOrderOffer).count(), 2)

    def test_any_shared_category_blocks_an_overlapping_multi_category_offer(self):
        other = Category(restaurant_id=self.restaurant.id, name="Desserts")
        self.db.add(other)
        self.db.commit()
        create_offer(self.payload("MAINS"), self.manager(), self.db)
        with self.assertRaises(HTTPException) as error:
            create_offer(self.payload("BOTH", eligible_category_ids=[self.food.category_id, other.id]), self.manager(), self.db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Mains", error.exception.detail)
        self.assertEqual(self.db.query(FoodOrderOffer).count(), 1)
