import importlib.util
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests import test_offer_discounts as fixtures
from app.models.dining import DiningTable, RestaurantFloor
from app.models.orders import FoodOrder
from app.models.offers import FoodOrderOffer, OfferRedemption
from app.models.realtime import RealtimeEvent
from app.models.revenue import PlatformCommission
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount
from app.routers.commerce.orders import (
    existing_spot_order, manager_create_spot_order, OrderLineInput, SpotOrderInput,
    CHEF_TRANSITIONS, MANAGER_TRANSITIONS, manager_mark_order_paid, update_status,
)


class GuestOrderIdempotencyTests(unittest.TestCase):
    setUp = fixtures.OfferCheckoutTests.setUp
    tearDown = fixtures.OfferCheckoutTests.tearDown

    def table(self, number='T1'):
        floor = self.db.query(RestaurantFloor).first()
        if floor is None:
            floor = RestaurantFloor(restaurant_id=self.restaurant.id, floor_name='Main', floor_number=1)
            self.db.add(floor)
            self.db.flush()
        table = DiningTable(restaurant_id=self.restaurant.id, floor_id=floor.id,
                            table_number=number, capacity=2, status='available')
        self.db.add(table)
        self.db.commit()
        return table

    def payload(self, table):
        return SpotOrderInput(idempotency_key=uuid.uuid4(), table_id=table.id, guest_name='Guest',
                              items=[OrderLineInput(variant_id=self.variant.id, quantity=1)])

    def create(self, payload):
        manager = self.db.get(User, self.restaurant.manager_id)
        return manager_create_spot_order(payload, manager, self.db)

    def order(self, key, customer_id=None):
        return FoodOrder(customer_id=customer_id, restaurant_id=self.restaurant.id,
                         idempotency_key=key, items=[], subtotal=0, total=0)

    def test_reservation_annotation_can_be_evaluated(self):
        self.assertIn('reservation', FoodOrder.__annotations__)
        self.assertEqual(FoodOrder.reservation.property.mapper.class_.__name__, 'Reservation')

    def test_duplicate_guest_key_is_rejected_by_database(self):
        key = uuid.uuid4()
        self.db.add(self.order(key))
        self.db.commit()
        self.db.add(self.order(key))
        with self.assertRaises(IntegrityError):
            self.db.flush()
        self.db.rollback()
        self.assertEqual(self.db.query(FoodOrder).filter_by(idempotency_key=key).count(), 1)

    def test_customer_keys_and_missing_guest_keys_remain_valid(self):
        key = uuid.uuid4()
        manager_id = self.restaurant.manager_id
        self.db.add_all([
            self.order(key), self.order(key, self.customer.id), self.order(key, manager_id),
            self.order(None), self.order(None),
        ])
        self.db.commit()
        self.assertEqual(self.db.query(FoodOrder).count(), 5)

    def test_customer_key_does_not_collide_with_guest_checkout(self):
        payload = self.payload(self.table())
        self.db.add(self.order(payload.idempotency_key, self.customer.id))
        self.db.commit()
        created = self.create(payload)
        self.assertEqual(self.create(payload)['id'], created['id'])
        self.assertEqual(self.db.query(FoodOrder).count(), 2)

    def test_retry_rechecks_key_after_waiting_for_table_lock(self):
        payload = self.payload(self.table())
        created = self.create(payload)
        # Simulate the initial SELECT running before the winning checkout commits.
        with patch('app.routers.commerce.orders.existing_spot_order',
                   side_effect=[None, self.db.get(FoodOrder, uuid.UUID(created['id']))]):
            retry = self.create(payload)
        self.assertEqual(retry['id'], created['id'])
        self.assertEqual(self.db.query(FoodOrder).count(), 1)

    def test_unique_violation_returns_winning_order(self):
        table = self.table()
        payload = self.payload(table)
        created = self.create(payload)
        # Make checkout proceed to INSERT, where the real unique index rejects it.
        table.status = 'available'
        self.db.commit()
        lookups = 0

        def concurrent_lookup(*args):
            nonlocal lookups
            lookups += 1
            return None if lookups <= 2 else existing_spot_order(*args)

        with patch('app.routers.commerce.orders.existing_spot_order', side_effect=concurrent_lookup):
            retry = self.create(payload)
        self.assertEqual(lookups, 3)
        self.assertEqual(retry['id'], created['id'])
        self.assertEqual(self.db.query(FoodOrder).count(), 1)

    def test_concurrent_key_reuse_for_another_table_returns_conflict(self):
        payload = self.payload(self.table())
        created = self.create(payload)
        other_table = self.table('T2')
        conflicting = payload.model_copy(update={'table_id': other_table.id})
        lookups = 0

        def concurrent_lookup(*args):
            nonlocal lookups
            lookups += 1
            return None if lookups <= 2 else existing_spot_order(*args)

        with patch('app.routers.commerce.orders.existing_spot_order', side_effect=concurrent_lookup):
            with self.assertRaises(HTTPException) as caught:
                self.create(conflicting)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(lookups, 3)
        self.assertEqual(self.db.get(DiningTable, other_table.id).status, 'available')
        self.assertEqual(str(self.db.query(FoodOrder).one().id), created['id'])

    def test_unrelated_integrity_error_is_not_hidden(self):
        payload = self.payload(self.table())
        error = IntegrityError('unrelated constraint', {}, Exception('constraint failure'))
        with patch('app.routers.commerce.orders.spot_order_totals', side_effect=error):
            with self.assertRaises(IntegrityError) as caught:
                self.create(payload)
        self.assertIs(caught.exception, error)

    def takeaway_payload(self, **overrides):
        values = dict(idempotency_key=uuid.uuid4(), service_type='TAKEAWAY', guest_name='Guest',
                      items=[OrderLineInput(variant_id=self.variant.id, quantity=1)])
        values.update(overrides)
        return SpotOrderInput(**values)

    def test_takeaway_can_be_created_without_any_tables(self):
        created = self.create(self.takeaway_payload())
        self.assertEqual(created['service_type'], 'TAKEAWAY')
        self.assertEqual(created['status'], 'ACCEPTED')
        self.assertIsNotNone(created['accepted_at'])
        self.assertIsNone(created['table_id'])
        self.assertIsNone(created['reservation_id'])
        self.assertEqual(created['tables'], [])
        self.assertEqual((created['subtotal'], created['tax_amount'], created['total']), (350, 63, 413))

    def test_service_type_validates_table_requirements(self):
        base = dict(idempotency_key=uuid.uuid4(), guest_name='Guest',
                    items=[dict(variant_id=self.variant.id, quantity=1)])
        for overrides in (
            {}, dict(service_type='DINE_IN'), dict(service_type='DINEIN'),
            dict(service_type='TAKEAWAY', table_id=uuid.uuid4()),
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                SpotOrderInput(**base, **overrides)
        payload = SpotOrderInput(**base, table_id=uuid.uuid4())
        self.assertEqual(payload.service_type, 'DINE_IN')

    def test_takeaway_offer_retry_does_not_duplicate_redemption_or_events(self):
        offer = FoodOrderOffer(restaurant_id=self.restaurant.id, code='TAKE10',
                               discount_percent=10, max_uses=1)
        self.db.add(offer)
        self.db.commit()
        payload = self.takeaway_payload(offer_code=offer.code)
        created = self.create(payload)
        retry = self.create(payload)
        self.assertEqual(created['id'], retry['id'])
        self.assertEqual(created['total'], 371.7)
        self.assertEqual(offer.uses, 1)
        self.assertEqual(self.db.query(OfferRedemption).count(), 1)
        self.assertEqual(self.db.query(RealtimeEvent).filter_by(resource_id=created['id']).count(), 1)

    def test_takeaway_unique_violation_recovers_winning_order(self):
        payload = self.takeaway_payload()
        created = self.create(payload)
        lookups = 0

        def concurrent_lookup(*args):
            nonlocal lookups
            lookups += 1
            return None if lookups == 1 else existing_spot_order(*args)

        with patch('app.routers.commerce.orders.existing_spot_order', side_effect=concurrent_lookup):
            retry = self.create(payload)
        self.assertEqual(lookups, 2)
        self.assertEqual(created['id'], retry['id'])
        self.assertEqual(self.db.query(FoodOrder).count(), 1)

    def test_checkout_key_cannot_change_service_type(self):
        payload = self.takeaway_payload()
        self.create(payload)
        conflicting = self.payload(self.table()).model_copy(update={'idempotency_key': payload.idempotency_key})
        with self.assertRaises(HTTPException) as caught:
            self.create(conflicting)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.query(FoodOrder).count(), 1)

    def test_database_rejects_takeaway_with_table_or_reservation(self):
        table_id = self.table().id
        reservation_id = self.reservation.id
        for fields in (dict(table_id=table_id), dict(reservation_id=reservation_id)):
            with self.subTest(fields=fields):
                row = self.order(uuid.uuid4())
                row.service_type = 'TAKEAWAY'
                for key, value in fields.items():
                    setattr(row, key, value)
                self.db.add(row)
                with self.assertRaises(IntegrityError):
                    self.db.flush()
                self.db.rollback()

    def test_takeaway_kitchen_completion_and_payment_leave_occupied_tables_alone(self):
        table = self.table()
        table.status = 'occupied'
        manager = self.db.get(User, self.restaurant.manager_id)
        self.db.add_all([
            User(name='Admin', email='takeaway-admin@example.test', password_hash='x', role=UserRole.ADMIN),
            WalletAccount(user_id=manager.id, balance=1000),
        ])
        self.db.commit()
        created = self.create(self.takeaway_payload())
        order_id = uuid.UUID(created['id'])
        for status in ('PREPARING', 'READY'):
            update_status(order_id, status, CHEF_TRANSITIONS, manager, self.db)
        completed = update_status(order_id, 'COMPLETED', MANAGER_TRANSITIONS, manager, self.db)
        paid = manager_mark_order_paid(order_id, manager, self.db)
        manager_mark_order_paid(order_id, manager, self.db)
        self.assertEqual(completed['status'], 'COMPLETED')
        self.assertEqual(paid['payment_status'], 'PAID')
        self.assertEqual(paid['service_type'], 'TAKEAWAY')
        self.assertEqual(table.status, 'occupied')
        self.assertEqual(self.db.query(PlatformCommission).count(), 1)


class GuestOrderMigrationTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'alembic/versions/0047_guest_order_idempotency.py'
        spec = importlib.util.spec_from_file_location('guest_order_migration', path)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        self.engine = create_engine('sqlite://')
        self.connection = self.engine.connect()
        self.connection.execute(text(
            'CREATE TABLE food_orders (customer_id TEXT, idempotency_key TEXT)'
        ))
        self.operations = Operations(MigrationContext.configure(self.connection))

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def test_upgrade_enforces_guest_uniqueness_and_downgrade_removes_it(self):
        self.connection.execute(text("INSERT INTO food_orders VALUES (NULL, 'key'), ('customer', 'key')"))
        with patch.object(self.migration, 'op', self.operations):
            self.migration.upgrade()
            with self.assertRaises(IntegrityError):
                self.connection.execute(text("INSERT INTO food_orders VALUES (NULL, 'key')"))
            self.migration.downgrade()
        self.connection.execute(text("INSERT INTO food_orders VALUES (NULL, 'key')"))
        self.assertEqual(self.connection.scalar(text('SELECT COUNT(*) FROM food_orders')), 3)

    def test_upgrade_stops_on_duplicate_keys_and_preserves_orders(self):
        self.connection.execute(text("INSERT INTO food_orders VALUES (NULL, 'key'), (NULL, 'key')"))
        with patch.object(self.migration, 'op', self.operations):
            with self.assertRaisesRegex(RuntimeError, 'duplicate idempotency keys'):
                self.migration.upgrade()
        self.assertEqual(self.connection.scalar(text('SELECT COUNT(*) FROM food_orders')), 2)
        self.assertEqual(inspect(self.connection).get_indexes('food_orders'), [])


@unittest.skipUnless(os.environ.get('DINEBOOK_TEST_POSTGRES') == '1', 'Requires local PostgreSQL')
class OrderServiceTypeMigrationTests(unittest.TestCase):
    def setUp(self):
        from app.core.config import settings

        path = Path(__file__).resolve().parents[1] / 'alembic/versions/0048_order_service_type.py'
        spec = importlib.util.spec_from_file_location('order_service_type_migration', path)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        self.engine = create_engine(settings.database_url)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        # PostgreSQL resolves this temporary table before the real food_orders table.
        self.connection.execute(text(
            "CREATE TEMP TABLE food_orders (service_type VARCHAR(10) NOT NULL DEFAULT 'DINEIN', "
            "table_id TEXT, reservation_id TEXT) ON COMMIT DROP"
        ))
        self.connection.execute(text("INSERT INTO food_orders DEFAULT VALUES"))
        self.operations = Operations(MigrationContext.configure(self.connection))

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def test_upgrade_preserves_orders_changes_default_and_enforces_takeaway_rules(self):
        with patch.object(self.migration, 'op', self.operations):
            self.migration.upgrade()
        self.assertEqual(self.connection.scalar(text('SELECT service_type FROM food_orders')), 'DINE_IN')
        self.connection.execute(text('INSERT INTO food_orders DEFAULT VALUES'))
        self.assertEqual(self.connection.scalar(text(
            "SELECT COUNT(*) FROM food_orders WHERE service_type = 'DINE_IN'"
        )), 2)
        self.connection.execute(text("INSERT INTO food_orders (service_type) VALUES ('TAKEAWAY')"))
        for sql in (
            "INSERT INTO food_orders (service_type) VALUES ('DINEIN')",
            "INSERT INTO food_orders (service_type, table_id) VALUES ('TAKEAWAY', 'table')",
            "INSERT INTO food_orders (service_type, reservation_id) VALUES ('TAKEAWAY', 'reservation')",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(IntegrityError), self.connection.begin_nested():
                    self.connection.execute(text(sql))

    def test_downgrade_restores_existing_migration_default(self):
        with patch.object(self.migration, 'op', self.operations):
            self.migration.upgrade()
            self.migration.downgrade()
        self.connection.execute(text('INSERT INTO food_orders DEFAULT VALUES'))
        self.assertEqual(self.connection.scalar(text(
            "SELECT COUNT(*) FROM food_orders WHERE service_type = 'DINEIN'"
        )), 2)
