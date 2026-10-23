import unittest
from tests import sqlite_support
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.database import Base
from app.models.restaurant import Restaurant
from app.models.orders import FoodOrder
from app.models.reviews import Review, ReviewFlag, ReviewModerationEvent
from app.models.user import User, UserRole
from app.routers.restaurants.reviews import ActionPayload, ReviewPayload, FlagPayload, create_review, update_review, moderate, flag_review, manager_flag_review, manager_router, delete_own_review, public_reviews


class ReviewModerationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.manager = User(name="Manager", email=f"manager-{uuid.uuid4()}@example.com", password_hash="x", role=UserRole.MANAGER)
        self.customer = User(name="Customer", email=f"customer-{uuid.uuid4()}@example.com", password_hash="x", role=UserRole.CUSTOMER)
        self.admin = User(name="Admin", email=f"admin-{uuid.uuid4()}@example.com", password_hash="x", role=UserRole.ADMIN)
        self.db.add_all((self.manager, self.customer, self.admin)); self.db.flush()
        self.restaurant = Restaurant(manager_id=self.manager.id, name="Test", address="1 Main", city="Pune")
        self.db.add(self.restaurant); self.db.flush()
        self.order = FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id, items=[], subtotal=0, total=0, status="COMPLETED")
        self.db.add(self.order); self.db.flush()
        self.review = Review(customer_id=self.customer.id, restaurant_id=self.restaurant.id, order_id=self.order.id, rating=1, content="Bad experience")
        self.db.add(self.review); self.db.flush()
        self.flag = ReviewFlag(review_id=self.review.id, customer_id=self.customer.id, reason="SPAM")
        self.db.add(self.flag); self.db.commit()

    def tearDown(self):
        self.db.close(); Base.metadata.drop_all(self.engine); self.engine.dispose()

    def test_rejection_requires_reason_and_leaves_review_unchanged(self):
        with self.assertRaises(HTTPException) as caught:
            moderate(self.review.id, ActionPayload(action="REJECT"), self.admin, self.db)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.review.status, "PUBLISHED")

    def test_hide_records_audit_and_resolves_pending_flags(self):
        result = moderate(self.review.id, ActionPayload(action="HIDE", reason="Contains abusive content"), self.admin, self.db)
        self.assertEqual(result["status"], "HIDDEN")
        self.assertEqual(self.flag.status, "RESOLVED")
        event = self.db.query(ReviewModerationEvent).one()
        self.assertEqual((event.action, event.previous_status, event.new_status), ("HIDE", "PUBLISHED", "HIDDEN"))
        self.assertEqual(event.admin_id, self.admin.id)

    def test_dismissing_a_flag_does_not_change_review_status(self):
        result = moderate(self.review.id, ActionPayload(action="DISMISS_FLAG", flag_id=self.flag.id), self.admin, self.db)
        self.assertEqual(result["review_status"], "PUBLISHED")
        self.assertEqual(self.flag.status, "DISMISSED")
        self.assertEqual(self.review.status, "PUBLISHED")
        self.assertEqual(self.db.query(ReviewModerationEvent).count(), 0)

    def test_customer_can_review_completed_order_once(self):
        self.db.delete(self.flag); self.db.delete(self.review); self.db.commit()
        payload = ReviewPayload(order_id=self.order.id, rating=5, content="A lovely meal")
        created = create_review(payload, self.customer, self.db)
        self.assertEqual(created["content"], "A lovely meal")
        with self.assertRaises(HTTPException) as caught:
            create_review(payload, self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 409)
        another_order = FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id, items=[], subtotal=0, total=0, status="COMPLETED")
        self.db.add(another_order); self.db.commit()
        another_payload = ReviewPayload(order_id=another_order.id, rating=4, content="Second restaurant review")
        with self.assertRaises(HTTPException) as caught:
            create_review(another_payload, self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_customer_cannot_edit_another_customers_review(self):
        other = User(name="Other", email=f"other-{uuid.uuid4()}@example.com", password_hash="x", role=UserRole.CUSTOMER)
        self.db.add(other); self.db.commit()
        payload = ReviewPayload(order_id=self.order.id, rating=4, content="Changed review")
        with self.assertRaises(HTTPException) as caught:
            update_review(self.review.id, payload, other, self.db)
        self.assertEqual(caught.exception.status_code, 404)

    def test_non_completed_orders_cannot_be_reviewed(self):
        order = FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id, items=[], subtotal=0, total=0, status="PLACED")
        self.db.add(order); self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            create_review(ReviewPayload(order_id=order.id, rating=5, content="Too soon"), self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_customer_can_review_restaurant_without_order(self):
        self.db.delete(self.flag); self.db.delete(self.review); self.db.commit()
        created = create_review(ReviewPayload(restaurant_id=self.restaurant.id, rating=5, content="Wonderful restaurant"), self.customer, self.db)
        self.assertIsNone(created["order_id"])
        self.assertEqual(created["restaurant_id"], str(self.restaurant.id))

    def test_manager_can_flag_but_not_delete_a_review(self):
        result = manager_flag_review(self.review.id, FlagPayload(reason="ABUSIVE", explanation="Contains profanity"), self.manager, self.db)
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(self.review.status, "PUBLISHED")
        self.assertFalse(any(route.path.endswith("/{review_id}") and "DELETE" in route.methods for route in manager_router.routes))
        self.assertFalse(any(route.path.endswith("/{review_id}/reply") for route in manager_router.routes))

    def test_manager_flag_blocks_new_review_for_24_hours(self):
        manager_flag_review(self.review.id, FlagPayload(reason="ABUSIVE"), self.manager, self.db)
        with self.assertRaises(HTTPException) as caught:
            create_review(ReviewPayload(restaurant_id=self.restaurant.id, rating=5, content="A new review"), self.customer, self.db)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("24 hours", caught.exception.detail)

    def test_customer_can_submit_again_after_removed_review_cooldown(self):
        manager_flag = manager_flag_review(self.review.id, FlagPayload(reason="ABUSIVE"), self.manager, self.db)
        flag = self.db.query(ReviewFlag).filter_by(id=uuid.UUID(manager_flag["id"])).one()
        self.review.status = "REMOVED"
        flag.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        self.db.commit()
        created = create_review(ReviewPayload(restaurant_id=self.restaurant.id, rating=5, content="A new review"), self.customer, self.db)
        self.assertEqual(created["content"], "A new review")
        self.assertEqual(created["status"], "PUBLISHED")

    def test_only_pending_manager_flag_allows_admin_removal(self):
        with self.assertRaises(HTTPException) as caught:
            moderate(self.review.id, ActionPayload(action="DELETE", reason="Contains profanity"), self.admin, self.db)
        self.assertEqual(caught.exception.status_code, 409)
        manager_flag = manager_flag_review(self.review.id, FlagPayload(reason="ABUSIVE"), self.manager, self.db)
        flag = self.db.query(ReviewFlag).filter_by(id=uuid.UUID(manager_flag["id"])).one()
        result = moderate(self.review.id, ActionPayload(action="DELETE", reason="Contains abusive words", flag_id=flag.id), self.admin, self.db)
        self.assertEqual(result["status"], "REMOVED")

    def test_admin_removed_review_is_excluded_from_public_reviews(self):
        manager_flag = manager_flag_review(self.review.id, FlagPayload(reason="ABUSIVE"), self.manager, self.db)
        flag = self.db.query(ReviewFlag).filter_by(id=uuid.UUID(manager_flag["id"])).one()
        moderate(self.review.id, ActionPayload(action="DELETE", reason="Contains abusive words", flag_id=flag.id), self.admin, self.db)
        result = public_reviews(self.restaurant.id, self.db, limit=20, offset=0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertIsNone(result["average_rating"])
        self.assertEqual(self.review.status, "REMOVED")
        self.assertEqual(flag.status, "RESOLVED")

    def test_customer_can_delete_own_review_and_related_flags(self):
        result = delete_own_review(self.review.id, self.customer, self.db)
        self.assertEqual(result["message"], "Your review was deleted.")
        self.assertIsNone(self.db.get(Review, self.review.id))
        self.assertEqual(self.db.query(ReviewFlag).filter_by(review_id=self.review.id).count(), 0)

    def test_duplicate_report_returns_conflict_instead_of_database_error(self):
        other = User(name="Reporter", email="reporter@example.com", password_hash="x", role=UserRole.CUSTOMER)
        self.db.add(other); self.db.commit()
        flag_review(self.review.id, FlagPayload(reason="SPAM"), other, self.db)
        with self.assertRaises(HTTPException) as caught:
            flag_review(self.review.id, FlagPayload(reason="SPAM"), other, self.db)
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
