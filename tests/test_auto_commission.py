import asyncio
import hashlib
import hmac
import json
import unittest
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests import sqlite_support
from app.core.config import settings
from app.db.database import Base
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant
from app.models.revenue import PlatformCommission, PlatformCommissionPayout, PlatformRevenueSettings
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction
from app.reconcile_commissions import reconcile
from app.routers.commerce.orders import manager_mark_order_paid
from app.routers.commerce.revenue import (
    CommissionUpdate, manager_revenue, pay_admin_commission, update_commission, wallet_summary,
)
from app.routers.restaurants.reservations import (
    ReservationPaymentVerification, cancel_reservation, manager_cancel_reservation,
    manager_confirm_payment, manager_confirm_preorder_payment, verify_reservation_payment,
)
from app.routers.commerce.webhooks import razorpay_webhook
from app.services.commission import settings_row
from app.services.restaurant_wallet import (
    PREORDER_ORDER_NAMESPACE, record_restaurant_payment, reverse_restaurant_payment,
)


class AutomaticCommissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False, expire_on_commit=False)
        self.manager = User(name="Manager", email="manager@example.com", password_hash="x", role=UserRole.MANAGER)
        self.admin = User(name="Admin", email="admin@example.com", password_hash="x", role=UserRole.ADMIN)
        self.customer = User(name="Customer", email="customer@example.com", password_hash="x", role=UserRole.CUSTOMER)
        self.db.add_all([self.manager, self.admin, self.customer])
        self.db.flush()
        self.restaurant = Restaurant(manager_id=self.manager.id, name="Test", address="Main Street", city="Pune")
        self.db.add(self.restaurant)
        self.db.add(WalletAccount(user_id=self.customer.id, balance=Decimal("0.00")))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def balance(self, user):
        account = self.db.get(WalletAccount, user.id)
        return Decimal(account.balance) if account else Decimal("0.00")

    def fund_manager(self, amount):
        account = self.db.get(WalletAccount, self.manager.id)
        if account is None:
            account = WalletAccount(user_id=self.manager.id, balance=Decimal(amount))
            self.db.add(account)
        else:
            account.balance = Decimal(amount)
        self.db.commit()

    def reservation(self, *, fee="100.00", preorder="0.00", method="WALLET", paid=True):
        row = Reservation(user_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_date=datetime.now(timezone.utc).date(), start_time=time(18), end_time=time(19),
            number_of_guests=2, fee_amount=Decimal(fee), payment_method=method,
            payment_status="PAID" if paid else "PENDING", paid_at=datetime.now(timezone.utc) if paid else None,
            status="CONFIRMED" if paid else "PENDING_PAYMENT", preorder_total=Decimal(preorder),
            fulfillment_type="PREORDER" if Decimal(preorder) else "TABLE_ONLY",
            preorder_payment_method=method if Decimal(preorder) else None,
            preorder_payment_status=("PAID" if paid else "PENDING") if Decimal(preorder) else None,
            preorder_paid_at=datetime.now(timezone.utc) if paid and Decimal(preorder) else None)
        self.db.add(row)
        self.db.commit()
        return row

    def pay(self, row, component="fee", **kwargs):
        amount = row.fee_amount if component == "fee" else row.preorder_total
        record_restaurant_payment(self.db, self.restaurant.id, amount, "Customer payment",
            f"customer-payment:reservation:{row.id}:{component}", **kwargs)

    def order(self, *, reservation=None, preorder=False, method="PAY_AT_DESK"):
        row = FoodOrder(customer_id=self.customer.id, restaurant_id=self.restaurant.id,
            reservation_id=reservation.id if reservation else None,
            idempotency_key=uuid.uuid5(PREORDER_ORDER_NAMESPACE, str(reservation.id)) if preorder else uuid.uuid4(),
            items=[], subtotal=Decimal("1000.00"), total=Decimal("1000.00"), status="PLACED",
            payment_method=method, payment_status="PENDING")
        self.db.add(row)
        self.db.commit()
        return row

    def test_digital_payment_splits_once_and_records_both_wallets(self):
        row = self.reservation(fee="1000.00")
        self.pay(row)
        self.pay(row)
        self.db.commit()
        self.assertEqual(self.balance(self.manager), Decimal("900.00"))
        self.assertEqual(self.balance(self.admin), Decimal("100.00"))
        self.assertEqual(self.db.query(PlatformCommission).count(), 1)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 1)
        self.assertEqual(self.db.query(WalletTransaction).count(), 3)
        summary = manager_revenue(self.manager, self.db)
        self.assertEqual(summary["admin_commission_outstanding"], 0)
        self.assertEqual(summary["admin_commission_paid_out"], 100)

    def test_refund_uses_original_rate_and_recipient_and_is_idempotent(self):
        row = self.reservation(fee="1000.00")
        self.pay(row)
        self.db.commit()
        update_commission(CommissionUpdate(commission_percent=25), self.admin, self.db)
        self.admin.is_active = False
        self.db.commit()
        reference = f"customer-refund:reservation:{row.id}:fee"
        for _ in range(2):
            reverse_restaurant_payment(self.db, self.restaurant.id, row.fee_amount, "Refund", reference)
        self.db.commit()
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertIsNotNone(self.db.query(PlatformCommission).one().refunded_at)
        result = wallet_summary(self.db, [self.manager.id], settings_row(self.db))
        self.assertEqual(result["admin_commission_paid_out"], 0)
        self.assertEqual(result["admin_received_this_month"], 0)
        self.assertEqual(result["commission_payouts"][0]["refunded_amount"], 100)
        self.assertEqual(result["commission_payouts"][0]["net_amount"], 0)

    def test_reservation_refund_reverses_fee_and_preorder_separately(self):
        row = self.reservation(fee="100.00", preorder="1000.00")
        now = datetime.now(timezone.utc)
        row.created_at = now - timedelta(hours=24)
        self.pay(row)
        self.pay(row, "preorder")
        self.db.commit()
        with patch("app.routers.restaurants.reservations.datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            result = cancel_reservation(row.id, self.customer, self.db)
        self.assertEqual(result["wallet_refund"], 1100)
        self.assertEqual(row.status, "CANCELLED")
        self.assertEqual(row.payment_status, "REFUNDED")
        self.assertEqual(row.preorder_payment_status, "REFUNDED")
        self.assertEqual(self.balance(self.customer), 1100)
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)
        with self.assertRaises(HTTPException) as retry:
            manager_cancel_reservation(row.id, manager=self.manager, db=self.db)
        self.assertEqual(retry.exception.status_code, 409)
        self.assertEqual(self.db.query(WalletTransaction).filter_by(transaction_type="REFUND").count(), 1)

    def test_expired_fee_is_retained_when_preorder_is_refunded(self):
        row = self.reservation(fee="100.00", preorder="1000.00")
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        self.pay(row)
        self.pay(row, "preorder")
        self.db.commit()
        result = cancel_reservation(row.id, self.customer, self.db)
        self.assertEqual(result["wallet_refund"], 1000)
        self.assertEqual(row.payment_status, "PAID")
        self.assertEqual(row.preorder_payment_status, "REFUNDED")
        self.assertEqual(self.balance(self.customer), 1000)
        self.assertEqual(self.balance(self.manager), 90)
        self.assertEqual(self.balance(self.admin), 10)

    def test_legacy_wallet_preorder_without_separate_payment_fields_is_refunded(self):
        row = self.reservation(preorder="1000.00")
        row.preorder_payment_method = None
        row.preorder_payment_status = None
        row.preorder_paid_at = None
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        self.pay(row)
        self.pay(row, "preorder")
        self.db.commit()

        self.assertEqual(cancel_reservation(row.id, self.customer, self.db)["wallet_refund"], 1000)
        self.assertEqual(row.preorder_payment_method, "WALLET")
        self.assertEqual(row.preorder_payment_status, "REFUNDED")
        self.assertEqual(self.balance(self.customer), 1000)

    def test_manager_cancels_paid_desk_booking_and_funds_wallet_refund(self):
        row = self.reservation(fee="100.00", preorder="1000.00", method="PAY_AT_DESK")
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        self.fund_manager("1100.00")
        self.pay(row, credit_gross=False)
        self.pay(row, "preorder", credit_gross=False)
        self.db.commit()
        result = manager_cancel_reservation(row.id, manager=self.manager, db=self.db)
        self.assertEqual(result["wallet_refund"], 1100)
        self.assertEqual(self.balance(self.customer), 1100)
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)

    def test_manager_cancellation_refunds_only_paid_components(self):
        row = self.reservation(preorder="1000.00")
        row.preorder_payment_method = "PAY_AT_DESK"
        row.preorder_payment_status = "PENDING"
        row.preorder_paid_at = None
        self.pay(row)
        self.db.commit()
        result = manager_cancel_reservation(row.id, manager=self.manager, db=self.db)
        self.assertEqual(result["wallet_refund"], 100)
        self.assertEqual(row.payment_status, "REFUNDED")
        self.assertEqual(row.preorder_payment_status, "CANCELLED")
        self.assertEqual(self.balance(self.customer), 100)
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)

    def test_desk_order_records_outstanding_without_wallet_movements(self):
        self.fund_manager("200.00")
        row = self.order()
        manager_mark_order_paid(row.id, self.manager, self.db)
        manager_mark_order_paid(row.id, self.manager, self.db)
        self.assertEqual(self.balance(self.manager), 200)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(row.payment_status, "PAID")
        self.assertEqual(self.db.query(WalletTransaction).count(), 0)
        record = self.db.query(PlatformCommission).one()
        self.assertEqual(record.commission_amount, 100)
        self.assertIsNone(record.payout_id)

    def test_desk_confirmation_needs_no_digital_funds(self):
        row = self.order()
        manager_mark_order_paid(row.id, self.manager, self.db)
        self.assertEqual(row.payment_status, "PAID")
        self.assertEqual(self.db.query(PlatformCommission).count(), 1)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 0)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.balance(self.manager), 0)

    def test_generated_preorder_order_does_not_duplicate_receivable(self):
        self.fund_manager("200.00")
        reservation = self.reservation(fee="0.00", preorder="1000.00", method="PAY_AT_DESK")
        self.pay(reservation, "preorder", credit_gross=False)
        self.db.commit()
        order = self.order(reservation=reservation, preorder=True)
        manager_mark_order_paid(order.id, self.manager, self.db)
        self.assertEqual(self.balance(self.manager), 200)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.db.query(PlatformCommission).one().source_type, "PREORDER")

    def test_desk_reservation_records_fee_and_preorder_receivables(self):
        self.fund_manager("200.00")
        row = self.reservation(preorder="1000.00", method="PAY_AT_DESK", paid=False)
        with patch("app.routers.restaurants.reservations.send_reservation_confirmation"):
            manager_confirm_payment(row.id, self.manager, self.db)
        self.assertEqual(self.balance(self.manager), 200)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 0)
        self.assertEqual(self.db.query(PlatformCommission).count(), 2)

    def test_later_desk_preorder_confirmation_records_its_receivable(self):
        self.fund_manager("200.00")
        row = self.reservation(preorder="1000.00", method="PAY_AT_DESK")
        row.preorder_payment_status = "PENDING"
        self.db.commit()
        manager_confirm_preorder_payment(row.id, self.manager, self.db)
        self.assertEqual(self.balance(self.manager), 200)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertIsNone(self.db.query(PlatformCommission).one().payout_id)

    def test_missing_admin_rolls_back_gross_credit_and_commission(self):
        row = self.reservation(fee="1000.00")
        self.admin.is_active = False
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self.pay(row)
        self.assertEqual(caught.exception.status_code, 503)
        self.db.rollback()
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.db.query(PlatformCommission).count(), 0)
        self.assertEqual(self.db.query(WalletTransaction).count(), 0)

    def test_notification_failure_rolls_back_all_payment_entries(self):
        row = self.reservation(fee="1000.00")
        with patch("app.services.commission.notify_wallet_transaction", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.pay(row)
        self.db.rollback()
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.db.query(PlatformCommission).count(), 0)
        self.assertEqual(self.db.query(WalletTransaction).count(), 0)

    def test_zero_and_full_commission_and_half_up_rounding(self):
        for rate, gross, admin_amount in (("0", "100", "0"), ("100", "100", "100"), ("10", "0.05", "0.01")):
            with self.subTest(rate=rate, gross=gross):
                settings = settings_row(self.db)
                settings.commission_percent = Decimal(rate)
                self.db.commit()
                before_manager, before_admin = self.balance(self.manager), self.balance(self.admin)
                row = self.reservation(fee=gross)
                self.pay(row)
                self.db.commit()
                self.assertEqual(self.balance(self.admin) - before_admin, Decimal(admin_amount))
                self.assertEqual(self.balance(self.manager) - before_manager, Decimal(gross) - Decimal(admin_amount))

    def test_new_rate_applies_only_to_future_payments(self):
        first = self.reservation(fee="1000")
        self.pay(first)
        self.db.commit()
        update_commission(CommissionUpdate(commission_percent=20), self.admin, self.db)
        self.pay(first)
        second = self.reservation(fee="1000")
        self.pay(second)
        self.db.commit()
        self.assertEqual(self.balance(self.admin), 300)
        self.assertEqual(self.balance(self.manager), 1700)

    def test_revenue_get_does_not_create_or_transfer_commission(self):
        self.reservation(fee="1000")
        manager_revenue(self.manager, self.db)
        self.assertEqual(self.db.query(PlatformCommission).count(), 0)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 0)
        result = pay_admin_commission(self.manager, self.db)
        self.assertEqual(result["settled_count"], 0)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 0)

    def test_legacy_preview_is_read_only_and_apply_never_recredits_gross(self):
        self.reservation(fee="1000")
        self.fund_manager("1000")
        result = reconcile(self.db)
        self.assertEqual(result["commission_to_transfer"], "100.00")
        self.assertEqual(self.db.query(PlatformCommission).count(), 0)
        self.assertEqual(self.balance(self.manager), 1000)
        reconcile(self.db, apply=True, backfill_rate=Decimal("10"))
        self.assertEqual(self.balance(self.manager), 900)
        self.assertEqual(self.balance(self.admin), 100)
        self.assertEqual(self.db.query(WalletTransaction).count(), 2)
        result = reconcile(self.db, apply=True)
        self.assertEqual(result["pending_records"], 0)
        self.assertEqual(self.balance(self.admin), 100)

    def test_existing_unpaid_commission_keeps_its_rate_without_gross_recredit(self):
        row = self.reservation(fee="1000")
        self.fund_manager("1000")
        self.db.add(PlatformCommission(manager_id=self.manager.id, restaurant_id=self.restaurant.id,
            source_type="RESERVATION_FEE", source_id=row.id, gross_amount=Decimal("1000"),
            commission_percent=Decimal("5"), commission_amount=Decimal("50"),
            occurred_at=datetime.now(timezone.utc)))
        self.db.commit()
        self.pay(row)
        self.db.commit()
        self.assertEqual(self.balance(self.manager), 950)
        self.assertEqual(self.balance(self.admin), 50)

    def razorpay_callback(self, row, event_id, event_type="payment.captured"):
        body = json.dumps({"event": event_type, "payload": {"payment": {"entity": {
            "order_id": row.razorpay_order_id, "id": "pay_commission", "amount": 150000,
            "currency": "INR", "status": "captured",
        }}}}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        request = Request({"type": "http", "method": "POST", "path": "/api/webhooks/razorpay",
            "headers": [(b"x-razorpay-signature", signature.encode()),
                        (b"x-razorpay-event-id", event_id.encode())]}, receive)
        with patch.object(settings, "razorpay_webhook_secret", "test-secret"), patch(
            "app.routers.commerce.webhooks.send_reservation_confirmation"
        ):
            return asyncio.run(razorpay_webhook(request, self.db))

    def verify_razorpay(self, row):
        signature = hmac.new(b"test-secret", f"{row.razorpay_order_id}|pay_commission".encode(), hashlib.sha256).hexdigest()
        payload = ReservationPaymentVerification(razorpay_order_id=row.razorpay_order_id,
            razorpay_payment_id="pay_commission", razorpay_signature=signature)
        with patch.object(settings, "razorpay_key_secret", "test-secret"), patch(
            "app.routers.restaurants.reservations.razorpay_request",
            return_value={"order_id": row.razorpay_order_id, "amount": 150000, "currency": "INR", "status": "captured"},
        ):
            return verify_reservation_payment(row.id, payload, BackgroundTasks(), self.customer, self.db)

    def test_manager_refunds_older_razorpay_booking_to_a_new_wallet(self):
        row = self.reservation(fee="1000", preorder="500", method="RAZORPAY", paid=False)
        row.razorpay_order_id = "order_manager_refund"
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        self.db.delete(self.db.get(WalletAccount, self.customer.id))
        self.db.commit()
        self.verify_razorpay(row)

        result = manager_cancel_reservation(row.id, manager=self.manager, db=self.db)
        self.assertEqual(result["wallet_refund"], 1500)
        self.assertEqual(self.balance(self.customer), 1500)
        self.assertEqual(self.balance(self.manager), 0)
        self.assertEqual(self.balance(self.admin), 0)
        self.razorpay_callback(row, "late-failed", "payment.failed")
        self.assertEqual(self.razorpay_callback(row, "late-capture")["status"], "processed")
        self.assertEqual(row.payment_status, "REFUNDED")
        self.assertEqual(row.preorder_payment_status, "REFUNDED")
        self.assertEqual(self.balance(self.customer), 1500)

    def test_razorpay_webhook_then_verification_does_not_duplicate_settlement(self):
        row = self.reservation(fee="1000", preorder="500", method="RAZORPAY", paid=False)
        row.razorpay_order_id = "order_commission"
        self.db.commit()
        self.razorpay_callback(row, "captured-first")
        self.verify_razorpay(row)
        self.razorpay_callback(row, "captured-again")
        self.assertEqual(self.balance(self.manager), 1350)
        self.assertEqual(self.balance(self.admin), 150)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 2)
        self.assertEqual(self.db.query(WalletTransaction).count(), 6)

    def test_razorpay_verification_then_webhook_does_not_duplicate_settlement(self):
        row = self.reservation(fee="1000", preorder="500", method="RAZORPAY", paid=False)
        row.razorpay_order_id = "order_commission"
        self.db.commit()
        self.verify_razorpay(row)
        self.razorpay_callback(row, "captured-later")
        self.assertEqual(self.balance(self.manager), 1350)
        self.assertEqual(self.balance(self.admin), 150)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 2)

    def test_digital_payment_withholds_commission_even_with_existing_wallet_debt(self):
        self.fund_manager("-1500")
        row = self.reservation(fee="1000")
        self.pay(row)
        self.db.commit()
        self.assertEqual(self.balance(self.manager), -600)
        self.assertEqual(self.balance(self.admin), 100)

    def test_changed_amount_cannot_reuse_a_payment_identity(self):
        row = self.reservation(fee="1000")
        self.pay(row)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            record_restaurant_payment(self.db, self.restaurant.id, Decimal("2000"), "Wrong amount",
                f"customer-payment:reservation:{row.id}:fee")
        self.assertEqual(caught.exception.status_code, 409)
        self.db.rollback()
        self.assertEqual(self.balance(self.admin), 100)

    def test_reconciliation_does_not_charge_refunded_unpaid_legacy_commission(self):
        row = self.reservation(fee="1000")
        self.db.add(PlatformCommission(manager_id=self.manager.id, restaurant_id=self.restaurant.id,
            source_type="RESERVATION_FEE", source_id=row.id, gross_amount=Decimal("1000"),
            commission_percent=Decimal("10"), commission_amount=Decimal("100"),
            occurred_at=datetime.now(timezone.utc)))
        row.payment_status = "REFUNDED"
        self.db.commit()
        result = reconcile(self.db, apply=True)
        self.assertEqual(result["pending_records"], 0)
        self.assertEqual(result["refunded_records_to_reconcile"], 1)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.db.query(PlatformCommissionPayout).count(), 0)

    def test_reconciliation_refuses_unknown_historical_rates(self):
        self.reservation(fee="1000")
        self.fund_manager("1000")
        with self.assertRaises(ValueError):
            reconcile(self.db, apply=True)
        self.db.rollback()
        self.assertEqual(self.balance(self.manager), 1000)
        self.assertEqual(self.balance(self.admin), 0)
        self.assertEqual(self.db.query(PlatformCommission).count(), 0)
