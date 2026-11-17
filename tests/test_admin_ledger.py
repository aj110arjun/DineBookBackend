import asyncio
import csv
import io
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.database import get_db
from app.models.revenue import PlatformCommission, PlatformCommissionPayout
from app.models.user import AccountStatus, UserRole
from app.routers.admin.admin_ledger import router
from app.schemas.admin_dashboard import AdminPeriodQuery
from app.services.admin_dashboard import revenue
from tests.test_manager_dashboard import DashboardFixtures

NOW = datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)

PREFIX = "/api/admin/ledger"
PERIOD = "start_date=2026-11-14&end_date=2026-11-14"


class AdminLedgerTests(DashboardFixtures, unittest.TestCase):
    def commission(self, gross, commission, *, restaurant=None, at=NOW, refund=None, payout=None, source="FOOD_ORDER"):
        restaurant = restaurant or self.restaurant
        record = PlatformCommission(restaurant_id=restaurant.id, manager_id=restaurant.manager_id,
            source_id=uuid.uuid4(), source_type=source, gross_amount=Decimal(gross), commission_amount=Decimal(commission),
            commission_percent=Decimal("10"), occurred_at=at, refunded_at=refund, payout_id=payout.id if payout else None)
        self.db.add(record)
        self.db.flush()
        return record

    def payout(self, amount, *, restaurant=None, at=NOW):
        row = PlatformCommissionPayout(manager_id=(restaurant or self.restaurant).manager_id,
            admin_id=self.admin.id, amount=Decimal(amount), created_at=at)
        self.db.add(row)
        self.db.flush()
        return row

    def setUp(self):
        super().setUp()
        self.admin = self.user("Admin", UserRole.ADMIN)
        self.chef = self.user("Chef", UserRole.CHEF)
        self.other = self.other_restaurant
        self.commission("1000", "100", at=OLD)
        old_payout = self.payout("50", at=OLD)
        self.old_refund = self.commission("500", "50", at=OLD, refund=NOW + timedelta(hours=2), payout=old_payout)
        self.capture = self.commission("1000", "100")
        self.commission("200", "20", refund=NOW + timedelta(hours=1))
        self.foreign = self.commission("600", "60", restaurant=self.other, payout=self.payout("60", restaurant=self.other))
        batch = self.payout("100", at=NOW + timedelta(minutes=30))
        self.commission("300", "30", at=OLD, payout=batch)
        self.commission("700", "70", at=OLD, payout=batch)
        self.commission("90000", "9000", source="WALLET_TOPUP")
        self.db.commit()
        self.tokens = {name: create_access_token(str(getattr(self, name).id))[0]
                       for name in ("admin", "manager", "other_manager", "customer", "chef")}
        self.app = FastAPI()
        self.app.include_router(router)

        def database():
            with Session(self.engine) as db:
                yield db
        self.app.dependency_overrides[get_db] = database

    def request(self, suffix="", user="admin", query=PERIOD):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                headers = {"cookie": f"dinebook_access_token={self.tokens[user]}"} if user else {}
                return await client.get(f"{PREFIX}{suffix}?{query}", headers=headers)
        return asyncio.run(run())

    def test_platform_totals_and_balances_match_admin_dashboard_across_restaurants(self):
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["total"], 7)
        self.assertEqual(report["summary"], dict(opening_balance="200.00", gross="1800.00", refunds="700.00",
            generated="180.00", reversed="70.00", earned="110.00", collected="160.00", collection_reversals="50.00",
            collected_net="110.00", net="0.00", closing_balance="200.00"))
        self.assertEqual(report["items"][-1]["balance"], "200.00")
        self.assertEqual({item["restaurant_name"] for item in report["items"]}, {self.restaurant.name, self.other.name})
        self.assertEqual({item["kind"] for item in report["items"]}, {"COLLECTION", "REFUND", "SETTLEMENT"})
        analytics = revenue(self.db, AdminPeriodQuery(start_date="2026-11-14", end_date="2026-11-14"))["summary"]
        for field in ("gross", "refunds", "generated", "reversed", "earned", "collected", "collection_reversals", "collected_net"):
            self.assertEqual(Decimal(report["summary"][field]), analytics[field], field)
        self.assertEqual(Decimal(report["summary"]["closing_balance"]), analytics["outstanding_end"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("WALLET_TOPUP", response.text)

    def test_pagination_keeps_balances_and_all_period_totals(self):
        full = self.request().json()
        for page in range(1, full["total"] + 1):
            report = self.request(query=PERIOD + f"&page={page}&page_size=1").json()
            self.assertEqual(report["items"], [full["items"][page - 1]])
            self.assertEqual(report["summary"], full["summary"])

    def test_inclusive_ist_dates_and_empty_period_carry_forward(self):
        first = self.commission("10", "1", at=datetime(2026, 11, 13, 18, 30, tzinfo=timezone.utc))
        last = self.commission("10", "1", at=datetime(2026, 11, 14, 18, 29, 59, tzinfo=timezone.utc))
        outside = self.commission("10", "1", at=datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc))
        self.db.commit()
        response = self.request()
        self.assertIn(f"ORD-{first.source_id.hex[:8].upper()}", response.text)
        self.assertIn(f"ORD-{last.source_id.hex[:8].upper()}", response.text)
        self.assertNotIn(f"ORD-{outside.source_id.hex[:8].upper()}", response.text)
        empty = self.request(query="start_date=2026-12-01&end_date=2026-12-01").json()
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["summary"]["opening_balance"], "203.00")
        self.assertEqual(empty["summary"]["closing_balance"], "203.00")

    def test_only_active_authenticated_admins_can_read_or_export(self):
        for suffix, query in (("", PERIOD), ("/export", PERIOD + "&format=csv")):
            self.assertEqual(self.request(suffix, None, query).status_code, 401)
            for role in ("manager", "other_manager", "customer", "chef"):
                self.assertEqual(self.request(suffix, role, query).status_code, 401)
        self.admin.status = AccountStatus.INACTIVE
        self.db.commit()
        self.assertEqual(self.request().status_code, 401)
        self.assertEqual(self.request("/export", query=PERIOD + "&format=pdf").status_code, 401)

    def test_invalid_ranges_formats_and_unexpected_scope_are_rejected(self):
        for query in ("start_date=2026-11-14", "start_date=2026-11-15&end_date=2026-11-14",
                      "start_date=2026-02-30&end_date=2026-03-01", "start_date=2000-01-01&end_date=2026-11-14",
                      PERIOD + "&page_size=1001", PERIOD + f"&manager_id={self.manager.id}"):
            self.assertEqual(self.request(query=query).status_code, 422, query)
        self.assertEqual(self.request("/export", query=PERIOD + "&format=exe").status_code, 422)

    def test_csv_includes_every_event_with_literal_restaurant_names(self):
        self.restaurant.name = "=1+1 & Café"
        self.db.commit()
        response = self.request("/export", query=PERIOD + "&format=csv&page=2&page_size=1")
        self.assertEqual(response.status_code, 200, response.text)
        records = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(records[0], ["DineBook Platform Ledger"])
        self.assertEqual(len(records[8:]), 7)
        self.assertTrue(any(row[1] == "'=1+1 & Café" for row in records[8:]))
        self.assertEqual(records[-1][-1], "200.00")
        self.assertIn("-50.00", records[-1])
        self.assertIn('filename="platform-ledger_2026-11-14_2026-11-14.csv"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_excel_contains_numeric_amounts_dates_and_order_codes(self):
        response = self.request("/export", query=PERIOD + "&format=xlsx&page_size=1")
        self.assertEqual(response.status_code, 200, response.text)
        with ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIsNone(archive.testzip())
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertEqual(sheet.find("s:dimension", ns).attrib["ref"], "A1:K15")
            self.assertEqual(sheet.find(".//s:c[@r='K15']/s:v", ns).text, "200.00")
            self.assertEqual(sheet.find(".//s:c[@r='A9']", ns).attrib["s"], "2")
            self.assertEqual(sheet.findall(".//s:f", ns), [])
            self.assertIn(f"ORD-{self.capture.source_id.hex[:8].upper()}".encode(), archive.read("xl/worksheets/sheet1.xml"))
            self.assertNotIn(str(self.capture.source_id).encode(), archive.read("xl/worksheets/sheet1.xml"))
            self.assertIn(b"Platform Ledger", archive.read("xl/workbook.xml"))

    def test_pdf_paginates_and_preserves_restaurants_references_and_summary(self):
        for _ in range(5):
            self.commission("10", "1")
        self.db.commit()
        response = self.request("/export", query=PERIOD + "&format=pdf&page_size=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.content.startswith(b"%PDF-1.4"))
        for text in (b"/Count 2", b"Closing outstanding: 205.00", b"Page 2 of 2", self.restaurant.name.encode(), f"ORD-{self.foreign.source_id.hex[:8].upper()}".encode()):
            self.assertIn(text, response.content)

    def test_reads_never_write_and_oversized_exports_fail_explicitly(self):
        statements = []
        def record(conn, cursor, statement, *args):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        self.assertEqual(self.request().status_code, 200)
        for format in ("pdf", "xlsx", "csv"):
            self.assertEqual(self.request("/export", query=PERIOD + "&format=" + format).status_code, 200)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        with patch("app.services.admin_ledger.EXPORT_LIMIT", 1):
            for format in ("pdf", "xlsx", "csv"):
                response = self.request("/export", query=PERIOD + "&format=" + format)
                self.assertEqual(response.status_code, 422)
                self.assertIn("shorter range", response.text)
