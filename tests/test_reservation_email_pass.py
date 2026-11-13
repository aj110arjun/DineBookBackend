import unittest
import uuid
from datetime import date, time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image
from app.services.reservation_email import send_reservation_confirmation


class ReservationPassEmailTests(unittest.TestCase):
    def setUp(self):
        # Transport/attachment tests isolate eligibility; settings tests cover the real DB policy.
        eligibility = patch('app.services.reservation_email.customer_notification_allowed', return_value=True)
        eligibility.start()
        self.addCleanup(eligibility.stop)
        self.item = SimpleNamespace(
            id=uuid.UUID('30b2668d-1111-2222-3333-444444444444'),
            user_id=uuid.uuid4(), restaurant_id=uuid.uuid4(),
            status='CONFIRMED', payment_status='PAID', payment_method='RAZORPAY',
            confirmation_email_sent_at=None, fee_amount=550,
            reservation_date=date(2026, 10, 30), start_time=time(19), number_of_guests=2,
            fulfillment_type='PREORDER', preorder_items=[{'food_name': 'Dinner'}],
            preorder_total=2997.20, preorder_discount=0, preorder_tax_amount=0,
            preorder_payment_status='PAID', preorder_payment_method='RAZORPAY',
            preorder_offer_code=None,
        )
        self.db = MagicMock()
        queries = [MagicMock() for _ in range(4)]
        queries[0].filter.return_value.with_for_update.return_value.first.return_value = self.item
        queries[1].filter.return_value.first.return_value = SimpleNamespace(name='Arjun AJ', email='guest@example.com')
        queries[2].filter.return_value.first.return_value = SimpleNamespace(name='Test Restaurant 1')
        queries[3].filter.return_value.all.return_value = []
        self.db.query.side_effect = queries

    @patch('app.services.reservation_email.build_reservation_invoice_pdf', return_value=b'%PDF-test')
    @patch('app.services.reservation_email.send_branded_email')
    def test_confirmation_attaches_invoice_and_personalized_png(self, send, invoice):
        self.assertTrue(send_reservation_confirmation(self.db, self.item.id))
        args = send.call_args.kwargs
        self.assertEqual(args['to'], 'guest@example.com')
        pdf, png = args['attachments']
        self.assertEqual(pdf[1], b'%PDF-test')
        self.assertEqual(png[0], 'DineBook-pass-RES-30B2668D.png')
        self.assertEqual(png[2:], ('image', 'png'))
        image = Image.open(BytesIO(png[1]))
        self.assertEqual(image.size, (640, 860))
        image.verify()
        self.assertIsNotNone(self.item.confirmation_email_sent_at)
        self.db.commit.assert_called_once()

    @patch('app.services.reservation_email.send_branded_email')
    def test_unpaid_reservation_does_not_send(self, send):
        self.item.payment_status = 'PENDING'
        self.assertFalse(send_reservation_confirmation(self.db, self.item.id))
        send.assert_not_called()

    @patch('app.services.reservation_email.build_reservation_invoice_pdf', return_value=b'%PDF-test')
    @patch('app.services.reservation_email.send_branded_email', side_effect=RuntimeError('SMTP unavailable'))
    def test_failed_delivery_does_not_mark_sent(self, send, invoice):
        self.assertFalse(send_reservation_confirmation(self.db, self.item.id))
        self.assertIsNone(self.item.confirmation_email_sent_at)
        self.db.rollback.assert_called_once()
