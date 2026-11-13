import unittest
import uuid
from datetime import datetime, timedelta, time, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from tests.test_customer_profile import ASGIClient, multipart, picture
from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import Base, get_db
from app.main import app
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.restaurant_document import RestaurantDocument, RestaurantDocumentType
from app.models.restaurant_hours import RestaurantHours, RestaurantHoursException
from app.models.user import AccountStatus, User, UserRole
from app.services.manager_profile import restaurant_details

PATH = '/api/manager/profile'


class ManagerProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.manager = User(name='Manager Name', email='manager-profile@example.com', password_hash='private hash', role=UserRole.MANAGER,
                                phone='+91 9876543210', email_verified=True, avatar_public_id='private-avatar-id')
            self.other = User(name='Other Manager', email='other-manager@example.com', password_hash='private hash', role=UserRole.MANAGER, email_verified=True)
            self.customer = User(name='Customer', email='customer-profile@example.com', password_hash='private hash', role=UserRole.CUSTOMER, email_verified=True)
            db.add_all([self.manager, self.other, self.customer]); db.flush()
            self.restaurant = Restaurant(manager_id=self.manager.id, name='Real Kitchen', description='A real restaurant', cuisine_type='Indian', email='contact@example.test',
                phone='+91 9876543210', address='Main Street', city='Pune', state='Maharashtra', pin_code='411001', status=RestaurantStatus.APPROVED, latitude=18.52, longitude=73.85)
            self.other_restaurant = Restaurant(manager_id=self.other.id, name='Other Restaurant', address='Private Street', city='Delhi')
            db.add_all([self.restaurant, self.other_restaurant]); db.flush()
            self.branding = RestaurantDocument(restaurant_id=self.restaurant.id, document_type=RestaurantDocumentType.BRANDING_IMAGE,
                file_name='cover.png', file_path='https://res.cloudinary.com/demo/cover.png', created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.registration = RestaurantDocument(restaurant_id=self.restaurant.id, document_type=RestaurantDocumentType.BUSINESS_REGISTRATION,
                file_name='registration.pdf', file_path='https://res.cloudinary.com/demo/private-registration.pdf')
            db.add_all([self.branding, self.registration])
            db.commit()
        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        guard = patch.object(settings, 'rate_limit_enabled', False); guard.start(); self.addCleanup(guard.stop)
        self.client = ASGIClient()
        self.token = create_access_token(str(self.manager.id))[0]

    def request(self, method='GET', suffix='', payload=None, token=None, **kwargs):
        return self.client.request(method, PATH + suffix, token=token or self.token, payload=payload, **kwargs)

    def upload(self, suffix='/avatar', content=None, mime='image/png'):
        body, media = multipart(picture() if content is None else content, mime)
        return self.request('POST', suffix, content=body, content_type=media)

    def test_profile_contains_actual_manager_and_owned_restaurant_without_secrets(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        manager, restaurant = response.json['manager'], response.json['restaurant']
        self.assertEqual(manager['id'], str(self.manager.id))
        self.assertEqual(manager['role'], 'MANAGER')
        self.assertEqual(manager['phone'], '+91 9876543210')
        self.assertTrue(manager['updated_at'])
        self.assertFalse(manager['can_change_email'])
        self.assertEqual(restaurant['name'], 'Real Kitchen')
        self.assertEqual(restaurant['branding_image_url'], self.branding.file_path)
        self.assertEqual(restaurant['documents'][0]['file_name'], 'registration.pdf')
        self.assertIsNone(restaurant['operation']['is_open_now'])
        text = str(response.json)
        for hidden in ('password_hash', 'private hash', 'avatar_public_id', 'private-avatar-id', 'password_changed_at', 'private-registration.pdf', 'Other Restaurant', 'Private Street'):
            self.assertNotIn(hidden, text)

    def test_personal_updates_trim_persist_and_can_clear_only_personal_phone(self):
        result = self.request('PATCH', payload={'name': "  Anne   O'Neil ", 'phone': '+44 20 7946 0958'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['name'], "Anne O'Neil")
        self.assertEqual(result.json['role'], 'MANAGER')
        self.assertIsNone(self.request('PATCH', payload={'phone': ''}).json['phone'])
        with self.sessions() as db:
            self.assertEqual(db.get(User, self.other.id).name, 'Other Manager')
            self.assertEqual(db.get(Restaurant, self.restaurant.id).phone, '+91 9876543210')
            self.assertEqual(db.get(User, self.manager.id).email, 'manager-profile@example.com')

    def test_existing_legacy_email_can_be_displayed_but_not_edited(self):
        with self.sessions() as db:
            db.get(User, self.manager.id).email = 'manager@localhost'
            db.commit()
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['manager']['email'], 'manager@localhost')
        self.assertEqual(self.request('PATCH', payload={'email': 'replacement@example.com'}).status_code, 422)

    def test_restaurant_fields_are_owned_partial_and_separate_from_manager_identity(self):
        values = {'name': '  New   Kitchen ', 'description': '  New menu\nLocal produce  ', 'email': '  CONTACT@EXAMPLE.COM ', 'phone': '+91 98765 43210',
                  'cuisine_type': 'Indian, Italian', 'address': '  21 New Street ', 'city': 'Mumbai', 'state': 'Maharashtra', 'pin_code': '400001'}
        result = self.request('PATCH', '/restaurant', values)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['name'], 'New Kitchen')
        self.assertEqual(result.json['email'], 'contact@example.com')
        self.assertEqual(result.json['description'], 'New menu\nLocal produce')
        self.assertEqual(result.json['status'], 'APPROVED')
        self.assertEqual(result.json['latitude'], 18.52)
        self.assertIsNone(self.request('PATCH', '/restaurant', {'description': '', 'email': ''}).json['description'])
        with self.sessions() as db:
            self.assertEqual(db.get(Restaurant, self.other_restaurant.id).name, 'Other Restaurant')
            self.assertEqual(db.get(User, self.manager.id).name, 'Manager Name')
            self.assertEqual(db.get(User, self.manager.id).email, 'manager-profile@example.com')

    def test_mass_assignment_and_admin_fields_are_forbidden(self):
        for suffix, fields in [('', ['id', 'email', 'role', 'status', 'is_active', 'manager_id', 'password_hash', 'avatar_url', 'avatar_public_id', 'updated_at']),
                               ('/restaurant', ['id', 'manager_id', 'restaurant_id', 'status', 'approval_status', 'documents', 'latitude', 'longitude', 'created_at', 'logo_url'])]:
            for field in fields:
                with self.subTest(suffix=suffix, field=field):
                    self.assertEqual(self.request('PATCH', suffix, {field: str(self.other.id)}).status_code, 422)
        with self.sessions() as db:
            self.assertEqual(db.get(Restaurant, self.restaurant.id).status, RestaurantStatus.APPROVED)
            self.assertEqual(db.get(User, self.manager.id).role, UserRole.MANAGER)

    def test_invalid_personal_and_restaurant_values_are_rejected_atomically(self):
        for payload in ({'name': ' '}, {'name': None}, {'name': 'Name123'}, {'phone': 'abc'}, {'phone': '+91 0000000000'}):
            self.assertEqual(self.request('PATCH', payload=payload).status_code, 422)
        for payload in ({'name': 'x'}, {'name': None}, {'name': '<img>'}, {'name': 'A'*256}, {'address': ''}, {'city': None},
                        {'phone': None}, {'phone': '0000000'}, {'phone': '+91 12345'}, {'email': 'invalid'},
                        {'pin_code': '12345678901'}, {'pin_code': '../x'}, {'description': 'a'*2001}, {'state': 123}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request('PATCH', '/restaurant', payload).status_code, 422)
        self.assertEqual(self.request().json['restaurant']['name'], 'Real Kitchen')

    def test_manager_only_authentication_and_revocation(self):
        for method, suffix in [('GET', ''), ('PATCH', ''), ('PATCH', '/restaurant'), ('POST', '/avatar'), ('DELETE', '/avatar'), ('POST', '/restaurant/branding-image')]:
            self.assertEqual(self.client.request(method, PATH+suffix, payload={} if method=='PATCH' else None).status_code, 401)
            for user in [self.customer]:
                token = create_access_token(str(user.id))[0]
                self.assertEqual(self.request(method, suffix, {} if method=='PATCH' else None, token=token).status_code, 401)
        for role in (UserRole.ADMIN, UserRole.CHEF):
            with self.sessions() as db:
                db.get(User, self.customer.id).role = role
                db.commit()
            self.assertEqual(self.request(token=create_access_token(str(self.customer.id))[0]).status_code, 401)
        with self.sessions() as db:
            db.get(User, self.manager.id).password_changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
            db.commit()
        self.assertEqual(self.request().status_code, 401)

    def test_suspended_restaurant_cannot_view_or_update(self):
        with self.sessions() as db:
            db.get(Restaurant, self.restaurant.id).status = RestaurantStatus.SUSPENDED
            db.commit()
        self.assertEqual(self.request().status_code, 403)
        self.assertEqual(self.request('PATCH', '/restaurant', {'name': 'New Kitchen'}).status_code, 403)

    def test_missing_restaurant_is_an_empty_state_and_cannot_target_someone_else(self):
        with self.sessions() as db:
            db.query(RestaurantDocument).filter_by(restaurant_id=self.restaurant.id).delete()
            db.delete(db.get(Restaurant, self.restaurant.id)); db.commit()
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json['restaurant'])
        self.assertEqual(self.request('PATCH', '/restaurant', {'name': 'New Kitchen'}).status_code, 404)
        self.assertEqual(self.upload('/restaurant/branding-image').status_code, 404)

    def test_avatar_reuses_secure_validation_and_manager_storage_namespace(self):
        with patch.object(settings, 'cloudinary_cloud_name', 'demo'), patch.object(settings, 'cloudinary_api_key', 'key'), patch.object(settings, 'cloudinary_api_secret', 'secret'), \
             patch('app.services.customer_profile.upload_file', return_value={'public_id': 'manager/avatar', 'secure_url': 'https://res.cloudinary.com/demo/avatar.png'}) as upload, \
             patch('app.services.customer_profile.delete_file', return_value={'result': 'ok'}):
            result = self.upload()
            self.assertEqual(result.status_code, 200)
            self.assertEqual(upload.call_args.kwargs['folder'], f'dinebook/manager-avatars/{self.manager.id}')
            self.assertEqual(result.json['avatar_url'], 'https://res.cloudinary.com/demo/avatar.png')
            self.assertNotIn('avatar_public_id', result.json)
            self.assertIsNone(self.request('DELETE', '/avatar').json['avatar_url'])
        for suffix in ('/avatar', '/restaurant/branding-image'):
            self.assertEqual(self.upload(suffix, content=b'forged image').status_code, 400)
            self.assertEqual(self.upload(suffix, mime='image/svg+xml').status_code, 400)
            self.assertEqual(self.upload(suffix, content=b'x'*(5*1024*1024+1)).status_code, 413)

    def test_branding_replaces_primary_without_changing_legal_documents_or_other_images(self):
        with self.sessions() as db:
            db.add(RestaurantDocument(restaurant_id=self.restaurant.id, document_type=RestaurantDocumentType.BRANDING_IMAGE,
                file_name='interior.png', file_path='https://res.cloudinary.com/demo/interior.png'))
            db.commit()
        with patch.object(settings, 'cloudinary_cloud_name', 'demo'), patch.object(settings, 'cloudinary_api_key', 'key'), patch.object(settings, 'cloudinary_api_secret', 'secret'), \
             patch('app.services.manager_profile.upload_file', return_value={'public_id': 'restaurant/cover', 'secure_url': 'https://res.cloudinary.com/demo/new-cover.png'}):
            result = self.upload('/restaurant/branding-image')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['branding_image_url'], 'https://res.cloudinary.com/demo/new-cover.png')
        with self.sessions() as db:
            self.assertEqual(db.get(RestaurantDocument, self.registration.id).file_path, 'https://res.cloudinary.com/demo/private-registration.pdf')
            self.assertEqual(db.query(RestaurantDocument).filter_by(restaurant_id=self.restaurant.id).count(), 3)

    def test_branding_database_failure_deletes_only_new_asset_and_keeps_old_record(self):
        with patch.object(settings, 'cloudinary_cloud_name', 'demo'), patch.object(settings, 'cloudinary_api_key', 'key'), patch.object(settings, 'cloudinary_api_secret', 'secret'), \
             patch('app.services.manager_profile.upload_file', return_value={'public_id': 'new-asset', 'secure_url': 'https://res.cloudinary.com/demo/new.png'}), \
             patch('app.services.customer_profile.delete_file', return_value={'result': 'ok'}) as delete, patch.object(Session, 'commit', side_effect=SQLAlchemyError('private details')):
            result = self.upload('/restaurant/branding-image')
        self.assertEqual(result.status_code, 502)
        self.assertNotIn('private details', result.json['detail'])
        delete.assert_called_once_with('new-asset', resource_type='image')
        self.assertEqual(self.request().json['restaurant']['branding_image_url'], self.branding.file_path)

    def test_unconfigured_cloudinary_fails_without_changing_profile(self):
        with patch.object(settings, 'cloudinary_cloud_name', None):
            self.assertEqual(self.upload().status_code, 503)
            self.assertEqual(self.upload('/restaurant/branding-image').status_code, 503)

    def test_restaurant_save_failure_rolls_back_without_exposing_database_details(self):
        with patch.object(Session, 'commit', side_effect=SQLAlchemyError('private error')):
            result = self.request('PATCH', '/restaurant', {'name': 'New Name'})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('private error', result.json['detail'])
        self.assertEqual(self.request().json['restaurant']['name'], 'Real Kitchen')

    def test_operational_status_reuses_restaurant_schedule_and_exceptions(self):
        now = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)  # Friday 13:30 local
        with self.sessions() as db:
            restaurant = db.get(Restaurant, self.restaurant.id)
            db.add(RestaurantHours(restaurant_id=restaurant.id, day_of_week='friday', enabled=True, open_time=time(12), close_time=time(15)))
            db.flush()
            self.assertTrue(restaurant_details(db, restaurant, now=now).operation.is_open_now)
            db.add(RestaurantHoursException(restaurant_id=restaurant.id, start_date=now.date(), end_date=now.date(), is_closed=True))
            db.flush()
            self.assertFalse(restaurant_details(db, restaurant, now=now).operation.is_open_now)
