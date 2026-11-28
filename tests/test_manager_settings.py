import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient
from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.main import app
from app.models.customer_settings import CustomerSettings
from app.models.notifications import Notification, NotificationPreference
from app.models.realtime import RealtimeEvent
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.schemas.customer_settings import GeneralSettings
from app.schemas.manager_settings import ManagerNotificationSettings
from app.services.notifications import create_notification

PATH = '/api/manager/settings'


class ManagerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.manager = User(name='Manager', email='manager@example.test', password_hash='hash', role=UserRole.MANAGER, email_verified=True)
            self.other = User(name='Other manager', email='other@example.test', password_hash='hash', role=UserRole.MANAGER, email_verified=True)
            self.customer = User(name='Customer', email='customer@example.test', password_hash='hash', role=UserRole.CUSTOMER, email_verified=True)
            db.add_all([self.manager, self.other, self.customer])
            db.flush()
            self.restaurant = Restaurant(manager_id=self.manager.id, name='Manager restaurant', address='Street', city='Pune', status=RestaurantStatus.APPROVED)
            db.add(self.restaurant)
            db.commit()
        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        guard = patch.object(settings, 'rate_limit_enabled', False)
        guard.start()
        self.addCleanup(guard.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.manager.id))[0]

    def request(self, method='GET', section='', payload=None, token=None):
        return self.client.request(method, PATH + section, payload=payload, token=token or self.token)

    def test_defaults_create_one_settings_row_without_a_new_table(self):
        for _ in range(2):
            result = self.request()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json, {'notifications': ManagerNotificationSettings().model_dump(), 'general': GeneralSettings().model_dump()})
            self.assertEqual(result.headers['cache-control'], 'no-store')
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerSettings).count(), 1)
            self.assertEqual(db.query(NotificationPreference).count(), 13)
            self.assertIsNotNone(db.get(CustomerSettings, self.manager.id))

    def test_general_preferences_persist_and_do_not_change_another_account(self):
        values = {'theme': 'dark', 'language': 'en', 'date_format': 'YYYY-MM-DD', 'time_format': '24h', 'timezone': 'America/New_York'}
        result = self.request('PATCH', '/general', values)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['general'], values)
        self.assertEqual(self.request().json['general'], values)
        other = create_access_token(str(self.other.id))[0]
        self.assertEqual(self.request(token=other).json['general'], GeneralSettings().model_dump())
        self.assertIsNone(self.request('PATCH', '/general', {'timezone': None}).json['general']['timezone'])
        with self.sessions() as db:
            self.assertIsNone(db.get(CustomerSettings, self.customer.id))

    def test_notification_patch_is_partial_and_preserves_existing_category_opt_outs(self):
        with self.sessions() as db:
            db.add(NotificationPreference(user_id=self.manager.id, category='ORDER', enabled=False))
            db.commit()
        notifications = self.request().json['notifications']
        self.assertFalse(notifications['order_confirmation'])
        self.assertFalse(notifications['order_status_updates'])
        changed = self.request('PATCH', '/notifications', {'order_confirmation': True}).json['notifications']
        self.assertTrue(changed['order_confirmation'])
        self.assertFalse(changed['order_status_updates'])
        self.assertTrue(changed['in_app_enabled'])
        self.assertEqual(self.request().json['notifications'], changed)

    def emit(self, db, kind, category, metadata=None):
        return create_notification(db, db.get(User, self.manager.id), notification_type=kind, category=category,
                                   title='Manager update', message='Restaurant activity', metadata=metadata)

    def test_each_notification_switch_controls_delivery_and_realtime_events(self):
        cases = [
            ('reservation_confirmation', 'reservation.confirmed', 'RESERVATION'),
            ('reservation_reminders', 'reservation.reminder', 'RESERVATION'),
            ('reservation_updates', 'reservation.cancelled', 'RESERVATION'),
            ('order_confirmation', 'order.placed', 'ORDER'),
            ('order_status_updates', 'order.preparing', 'ORDER'),
            ('order_completion', 'order.completed', 'ORDER'),
            ('payment_confirmation', 'payment.success', 'PAYMENT'),
            ('refund_updates', 'payment.refunded', 'PAYMENT'),
            ('wallet_transactions', 'wallet.transaction', 'PAYMENT'),
            ('review_updates', 'review.moderated', 'REVIEW'),
            ('staff_updates', 'staff.updated', 'STAFF'),
            ('approval_updates', 'restaurant.approved', 'APPROVAL'),
        ]
        for field, kind, category in cases:
            with self.subTest(field=field):
                self.assertEqual(self.request('PATCH', '/notifications', {field: False}).status_code, 200)
                with self.sessions() as db:
                    self.assertIsNone(self.emit(db, kind, category))
                    self.assertEqual(db.query(Notification).count(), 0)
                    self.assertEqual(db.query(RealtimeEvent).count(), 0)
                self.request('PATCH', '/notifications', {field: True})
                with self.sessions() as db:
                    self.assertIsNotNone(self.emit(db, kind, category))
                    self.assertEqual(db.query(RealtimeEvent).count(), 1)

    def test_global_opt_out_preserves_security_and_failed_payment_notices(self):
        self.request('PATCH', '/notifications', {'in_app_enabled': False})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, 'order.placed', 'ORDER'))
            self.assertIsNone(self.emit(db, 'wallet.transaction', 'PAYMENT'))
            for kind, category in [('security.alert', 'SYSTEM'), ('restaurant.suspended', 'SYSTEM'), ('payment.failed', 'PAYMENT'), ('legal.receipt', 'PAYMENT')]:
                self.assertIsNotNone(self.emit(db, kind, category))

    def test_refund_metadata_uses_the_refund_switch(self):
        self.request('PATCH', '/notifications', {'refund_updates': False, 'wallet_transactions': True})
        with self.sessions() as db:
            self.assertIsNone(self.emit(db, 'wallet.transaction', 'PAYMENT', {'transaction_type': 'REFUND'}))
            self.assertIsNotNone(self.emit(db, 'wallet.transaction', 'PAYMENT', {'transaction_type': 'CREDIT'}))

    def test_legacy_category_api_and_manager_preferences_stay_consistent(self):
        self.request()
        result = self.client.request('PATCH', '/api/notifications/preferences', token=self.token, payload={'preferences': {'ORDER': False, 'STAFF': False}})
        self.assertEqual(result.status_code, 200)
        notifications = self.request().json['notifications']
        self.assertFalse(notifications['order_completion'])
        self.assertFalse(notifications['staff_updates'])
        self.request('PATCH', '/notifications', {'staff_updates': True})
        result = self.client.request('GET', '/api/notifications/preferences', token=self.token)
        self.assertTrue(result.json['preferences']['STAFF'])
        self.assertFalse(result.json['preferences']['ORDER'])

    def test_authentication_roles_and_suspended_managers_are_enforced(self):
        for method, section in [('GET', ''), ('PATCH', '/general'), ('PATCH', '/notifications')]:
            self.assertEqual(self.client.request(method, PATH + section, payload={} if method == 'PATCH' else None).status_code, 401)
            self.assertEqual(self.request(method, section, {}, token='invalid').status_code, 401)
            self.assertEqual(self.request(method, section, {}, token=create_access_token(str(self.customer.id))[0]).status_code, 401)
        with self.sessions() as db:
            db.get(Restaurant, self.restaurant.id).status = RestaurantStatus.SUSPENDED
            db.commit()
        self.assertEqual(self.request().status_code, 403)

    def test_invalid_values_nulls_and_account_mass_assignment_are_rejected(self):
        for section, payload in [('/general', {'theme': 'invalid'}), ('/general', {'theme': None}), ('/general', {'language': 'fr'}),
                ('/general', {'date_format': 'D/M'}), ('/general', {'time_format': '25h'}), ('/general', {'timezone': 'Mars/Olympus'}),
                ('/general', {'user_id': str(self.other.id)}), ('/general', {'customer_id': str(self.other.id)}),
                ('/notifications', {'in_app_enabled': 'false'}), ('/notifications', {'in_app_enabled': None}),
                ('/notifications', {'security_enabled': False}), ('/notifications', {'manager_id': str(self.other.id)})]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request('PATCH', section, payload).status_code, 422)

    def test_database_failure_rolls_back_and_does_not_expose_details(self):
        self.request()
        with patch.object(Session, 'commit', side_effect=SQLAlchemyError('private data')):
            result = self.request('PATCH', '/general', {'theme': 'dark'})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('private', result.json['detail'])
        self.assertEqual(self.request().json['general']['theme'], 'system')
