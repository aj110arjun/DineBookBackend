import asyncio
import importlib.util
import unittest
import uuid
from datetime import date, time
from pathlib import Path

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import Column, ForeignKey, MetaData, String, Table, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.database import get_db
from app.models.dining import DiningTable, FloorZone, RestaurantFloor
from app.routers.restaurants.dining import manager_router
from app.routers.restaurants.reservations import floor_layout_data
from tests.test_manager_dashboard import DashboardFixtures


class FloorZoneTests(DashboardFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db.commit()
        self.db.execute(text("PRAGMA foreign_keys=ON"))
        self.floor = self.add_floor(self.restaurant, 1)
        self.upstairs = self.add_floor(self.restaurant, 2)
        self.foreign = self.add_floor(self.other_restaurant, 1)
        self.zone = FloorZone(floor_id=self.floor.id, name="Window Seats")
        self.foreign_zone = FloorZone(floor_id=self.foreign.id, name="Private Zone")
        self.db.add_all([self.zone, self.foreign_zone])
        self.db.commit()
        self.app = FastAPI()
        self.app.include_router(manager_router)

        def database():
            with Session(self.engine) as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        self.token = create_access_token(str(self.manager.id))[0]

    def add_floor(self, restaurant, number):
        row = RestaurantFloor(restaurant_id=restaurant.id, floor_number=number, floor_name=f"Floor {number}")
        self.db.add(row)
        self.db.flush()
        return row

    def request(self, method, path, payload=None, token=True):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.request(method, f"/api/manager{path}", json=payload,
                    headers={"cookie": f"dinebook_access_token={self.token}"} if token else {})
        return asyncio.run(run())

    def zone_path(self, zone=None, floor=None):
        path = f"/floors/{(floor or self.floor).id}/zones"
        return f"{path}/{zone.id}" if zone else path

    def table_payload(self, **changes):
        return dict(floor_id=str(self.floor.id), table_number="T-1", capacity=4, grid_row=0, grid_col=0,
                    zone_id=str(self.zone.id), **changes)

    def test_create_floor_with_optional_zones_is_atomic_and_normalized(self):
        response = self.request("POST", "/floors", dict(floor_number=3, floor_name="Terrace", zones=["  Main   Dining ", "Window Seats"]))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual({zone["name"] for zone in response.json()["zones"]}, {"Main Dining", "Window Seats"})
        self.assertEqual(response.json()["tables"], [])
        for names in (["VIP", " vip "], [" "], ["x" * 101]):
            rejected = self.request("POST", "/floors", dict(floor_number=4, floor_name="Invalid floor", zones=names))
            self.assertEqual(rejected.status_code, 422, rejected.text)
        self.db.expire_all()
        self.assertIsNone(self.db.query(RestaurantFloor).filter_by(restaurant_id=self.restaurant.id, floor_number=4).first())

    def test_zone_crud_and_case_insensitive_names_are_scoped_to_floor(self):
        created = self.request("POST", self.zone_path(), {"name": "  Main   Dining "})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["name"], "Main Dining")
        duplicate = self.request("POST", self.zone_path(), {"name": "main dining"})
        self.assertEqual(duplicate.status_code, 409)
        renamed = self.request("PATCH", self.zone_path(self.zone), {"name": "Balcony"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Balcony")
        self.assertEqual(self.request("DELETE", self.zone_path(self.zone)).status_code, 200)
        self.assertEqual(self.request("POST", self.zone_path(floor=self.upstairs), {"name": "Main Dining"}).status_code, 201)

    def test_tables_persist_zone_and_readers_receive_zone_metadata(self):
        created = self.request("POST", "/tables", self.table_payload())
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["zone_name"], "Window Seats")
        self.assertEqual(created.json()["zone_id"], str(self.zone.id))
        floors = self.request("GET", "/floors").json()
        floor = next(row for row in floors if row["id"] == str(self.floor.id))
        self.assertEqual(floor["zones"][0]["name"], "Window Seats")
        self.assertEqual(floor["tables"][0]["zone_name"], "Window Seats")
        layout = floor_layout_data(self.db, self.restaurant.id, date(2035, 1, 2), time(18))
        self.assertEqual(layout[0]["zone_id"], str(self.zone.id))
        self.assertEqual(layout[0]["zone_name"], "Window Seats")

    def test_cross_floor_and_cross_restaurant_assignments_and_zone_writes_are_rejected(self):
        for floor in (self.upstairs, self.foreign):
            payload = self.table_payload()
            payload["floor_id"] = str(floor.id)
            self.assertEqual(self.request("POST", "/tables", payload).status_code, 404)
        payload = self.table_payload()
        payload["zone_id"] = str(self.foreign_zone.id)
        self.assertEqual(self.request("POST", "/tables", payload).status_code, 404)
        for method in ("PATCH", "DELETE"):
            self.assertEqual(self.request(method, self.zone_path(self.foreign_zone, self.foreign), {"name": "Changed"} if method == "PATCH" else None).status_code, 404)
        self.assertEqual(self.request("POST", self.zone_path(floor=self.foreign), {"name": "Intruder"}).status_code, 404)
        self.assertEqual(self.request("POST", self.zone_path(), {"name": "Guest"}, token=False).status_code, 401)

    def test_zone_can_be_cleared_and_moving_a_table_clears_previous_floor_zone(self):
        table = self.request("POST", "/tables", self.table_payload()).json()
        path = f"/tables/{table['id']}"
        cleared = self.request("PATCH", path, {"zone_id": None})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["zone_name"])
        self.assertEqual(self.request("PATCH", path, {"zone_id": str(self.zone.id)}).status_code, 200)
        moved = self.request("PATCH", path, {"floor_id": str(self.upstairs.id)})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertIsNone(moved.json()["zone_id"])
        self.assertEqual(self.request("PATCH", path, {"zone_id": str(self.zone.id)}).status_code, 404)
        self.assertEqual(self.request("PATCH", path, {"capacity": None}).status_code, 422)

    def test_assigned_zone_cannot_be_deleted_until_its_tables_are_moved(self):
        table = self.request("POST", "/tables", self.table_payload()).json()
        self.assertEqual(self.request("DELETE", self.zone_path(self.zone)).status_code, 409)
        self.assertEqual(self.request("PATCH", f"/tables/{table['id']}", {"zone_id": None}).status_code, 200)
        self.assertEqual(self.request("DELETE", self.zone_path(self.zone)).status_code, 200)

    def test_legacy_unzoned_tables_remain_available_and_database_rejects_cross_floor_zone(self):
        payload = self.table_payload()
        payload.pop("zone_id")
        response = self.request("POST", "/tables", payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()["zone_id"])
        layout = floor_layout_data(self.db, self.restaurant.id, date(2035, 1, 2), time(18))
        self.assertEqual(layout[0]["status"], "available")
        self.assertIsNone(layout[0]["zone_name"])
        table = self.db.get(DiningTable, uuid.UUID(response.json()["id"]))
        table.zone_id = self.foreign_zone.id
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()


class FloorZoneMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_preserve_existing_tables(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        floors = Table("restaurant_floors", metadata, Column("id", UUID(as_uuid=True), primary_key=True))
        tables = Table("dining_tables", metadata, Column("id", UUID(as_uuid=True), primary_key=True),
                       Column("floor_id", UUID(as_uuid=True), ForeignKey("restaurant_floors.id")), Column("table_number", String(30)))
        metadata.create_all(engine)
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0056_floor_zones.py"
        spec = importlib.util.spec_from_file_location("floor_zones_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        floor_id, table_id = uuid.uuid4(), uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(floors.insert().values(id=floor_id))
            connection.execute(tables.insert().values(id=table_id, floor_id=floor_id, table_number="T-1"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                self.assertIn("zone_id", {column["name"] for column in inspect(connection).get_columns("dining_tables")})
                self.assertIsNone(connection.execute(text("SELECT zone_id FROM dining_tables")).scalar())
                self.assertEqual(connection.execute(select(tables.c.table_number)).scalar(), "T-1")
                migration.downgrade()
                self.assertNotIn("zone_id", {column["name"] for column in inspect(connection).get_columns("dining_tables")})
                self.assertEqual(connection.execute(select(tables.c.table_number)).scalar(), "T-1")
