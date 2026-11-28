import asyncio
import csv
import io
import re
import unittest
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
from app.models.restaurant import RestaurantStatus
from app.models.user import UserRole
from app.routers.manager.manager_ledger import router
from app.services.manager_ledger_exports import pdf_text_width
from tests.test_manager_dashboard import DashboardFixtures, NOW

PREFIX = "/api/manager/ledger"
PERIOD = "start_date=2026-11-13&end_date=2026-11-13"


class ManagerLedgerTests(DashboardFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.own = self.collection(at=NOW, gross="1000", commission="100")
        self.collection(at=NOW, gross="200", commission="20", refunded_at=NOW + timedelta(hours=1))
        self.collection(at=NOW - timedelta(days=3), gross="100", commission="10")
        self.collection(at=NOW - timedelta(days=10), gross="300", commission="30", refunded_at=NOW + timedelta(hours=2))
        self.foreign = self.collection(restaurant=self.other_restaurant, gross="90000", commission="9000")
        self.chef = self.user("Chef", UserRole.CHEF)
        self.admin = self.user("Admin", UserRole.ADMIN)
        self.db.commit()
        self.tokens = {key: create_access_token(str(user.id))[0] for key, user in {
            "manager": self.manager, "other": self.other_manager, "customer": self.customer, "chef": self.chef, "admin": self.admin,
        }.items()}
        self.app = FastAPI(); self.app.include_router(router)
        def database():
            with Session(self.engine) as db: yield db
        self.app.dependency_overrides[get_db] = database

    def request(self, suffix="", user="manager", query=PERIOD):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                headers = {"cookie": f"dinebook_access_token={self.tokens[user]}"} if user else {}
                return await client.get(f"{PREFIX}{suffix}?{query}", headers=headers)
        return asyncio.run(run())

    def test_scoped_ledger_opening_totals_and_closing_match_dashboard(self):
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["summary"], {"opening_balance": "360.00", "collected": "1200.00",
            "refunded": "500.00", "commission": "70.00", "net": "630.00", "closing_balance": "990.00"})
        self.assertEqual(report["items"][-1]["balance"], "990.00")
        self.assertNotIn(f"ORD-{self.foreign.source_id.hex[:8].upper()}", response.text)
        dashboard = self.result(start="2026-11-13", end="2026-11-13")
        self.assertEqual(Decimal(report["summary"]["net"]), dashboard.revenue.net)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_pagination_keeps_the_running_balance_from_prior_pages(self):
        full = self.request().json()
        for page in (1, 2, 3, 4):
            response = self.request(query=PERIOD + f"&page={page}&page_size=1")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["items"][0], full["items"][page - 1])
            self.assertEqual(response.json()["summary"], full["summary"])

    def test_ist_date_boundaries_are_inclusive_and_next_midnight_is_excluded(self):
        first = self.collection(at=datetime(2026, 11, 12, 18, 30, tzinfo=timezone.utc), gross="11", commission="1")
        last = self.collection(at=datetime(2026, 11, 13, 18, 29, 59, tzinfo=timezone.utc), gross="12", commission="1")
        outside = self.collection(at=datetime(2026, 11, 13, 18, 30, tzinfo=timezone.utc), gross="13", commission="1")
        self.db.commit()
        response = self.request()
        self.assertIn(f"ORD-{first.source_id.hex[:8].upper()}", response.text)
        self.assertIn(f"ORD-{last.source_id.hex[:8].upper()}", response.text)
        self.assertNotIn(f"ORD-{outside.source_id.hex[:8].upper()}", response.text)

    def test_empty_period_still_reports_its_opening_and_closing_balance(self):
        report = self.request(query="start_date=2026-12-01&end_date=2026-12-01").json()
        self.assertEqual(report["items"], []); self.assertEqual(report["total"], 0)
        self.assertEqual(report["summary"]["opening_balance"], "990.00")
        self.assertEqual(report["summary"]["closing_balance"], "990.00")
        self.assertEqual(report["summary"]["net"], "0.00")

    def test_authentication_roles_ownership_and_suspension(self):
        self.assertEqual(self.request(user=None).status_code, 401)
        for role in ("customer", "chef", "admin"):
            self.assertIn(self.request(user=role).status_code, (401, 403))
            self.assertIn(self.request("/export", role, PERIOD + "&format=csv").status_code, (401, 403))
        report = self.request(user="other").json()
        self.assertEqual(report["total"], 1); self.assertEqual(report["summary"]["collected"], "90000.00")
        self.assertNotEqual(report["restaurant_name"], self.restaurant.name)
        self.restaurant.status = RestaurantStatus.SUSPENDED; self.db.commit()
        self.assertEqual(self.request().status_code, 403)

    def test_invalid_ranges_and_client_selected_restaurants_are_rejected(self):
        for query in ("start_date=2026-11-13", "start_date=2026-11-14&end_date=2026-11-13",
                      "start_date=2026-02-30&end_date=2026-03-01", "start_date=2000-01-01&end_date=2026-11-13",
                      PERIOD + f"&restaurant_id={self.other_restaurant.id}", PERIOD + "&page_size=1001"):
            self.assertEqual(self.request(query=query).status_code, 422, query)
        self.assertEqual(self.request("/export", query=PERIOD + "&format=exe").status_code, 422)

    def test_csv_exports_all_pages_with_exact_amounts_and_safe_text(self):
        self.restaurant.name = "=HYPERLINK(\"https://example.invalid\")"; self.db.commit()
        response = self.request("/export", query=PERIOD + "&format=csv&page=2&page_size=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        records = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertTrue(records[1][1].startswith("'="))
        self.assertEqual(len(records[8:]), 4)
        self.assertEqual(records[-1][-1], "990.00")
        self.assertEqual(records[5], ["360.00", "1200.00", "500.00", "70.00", "630.00", "990.00"])
        self.assertTrue(any(row[-3] == "-20.00" for row in records[8:]))
        self.assertIn('filename="restaurant-ledger_2026-11-13_2026-11-13.csv"', response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_excel_is_a_real_xlsx_with_numeric_amounts_dates_and_literal_text(self):
        self.restaurant.name = "=1+1 & Café"; self.db.commit()
        response = self.request("/export", query=PERIOD + "&format=xlsx&page_size=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("spreadsheetml.sheet", response.headers["content-type"])
        with ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIsNone(archive.testzip())
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertEqual(sheet.find("s:dimension", ns).attrib["ref"], "A1:I12")
            self.assertEqual(sheet.find(".//s:c[@r='A2']/s:is/s:t", ns).text, self.restaurant.name)
            self.assertEqual(sheet.findall(".//s:f", ns), [])
            self.assertEqual(sheet.find(".//s:c[@r='A9']", ns).attrib["s"], "2")
            self.assertEqual(sheet.find(".//s:c[@r='I12']/s:v", ns).text, "990.00")
            self.assertEqual(sheet.find(".//s:pane", ns).attrib["state"], "frozen")

    def test_pdf_is_paginated_and_contains_the_full_report(self):
        for _ in range(20): self.collection(at=NOW, gross="1", commission="0")
        self.db.commit()
        response = self.request("/export", query=PERIOD + "&format=pdf&page_size=1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF-1.4")); self.assertTrue(response.content.endswith(b"%%EOF\n"))
        self.assertIn(b"/Count 2", response.content)
        self.assertIn(b"Closing: 1,010.00", response.content)
        self.assertIn(f"ORD-{self.own.source_id.hex[:8].upper()}".encode(), response.content)
        self.assertNotIn(str(self.own.source_id).encode(), response.content)
        self.assertIn(b"Page 2 of 2", response.content)

    def test_pdf_amount_headers_and_values_share_right_aligned_column_edges(self):
        response = self.request("/export", query=PERIOD + "&format=pdf")
        self.assertEqual(response.status_code, 200, response.text)
        header = re.search(rb"1 0 0 1 ([\d.]+) 434 Tm \(Collected\) Tj ET", response.content)
        amount = re.search(rb"1 0 0 1 ([\d.]+) 414 Tm \(1,000\.00\) Tj ET", response.content)
        self.assertIsNotNone(header)
        self.assertIsNotNone(amount)

        collected_column_right_edge = 36 + 100 + 110 + 130 + 90 - 8
        self.assertAlmostEqual(
            float(header.group(1)) + pdf_text_width("Collected", 7),
            collected_column_right_edge,
        )
        self.assertAlmostEqual(
            float(amount.group(1)) + pdf_text_width("1,000.00", 7),
            collected_column_right_edge,
        )

    def test_no_business_writes_and_oversized_exports_never_silently_truncate(self):
        statements = []
        def record(conn, cursor, statement, *args): statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        self.assertEqual(self.request().status_code, 200)
        for format in ("pdf", "xlsx", "csv"):
            self.assertEqual(self.request("/export", query=PERIOD + "&format=" + format).status_code, 200)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        with patch("app.services.manager_ledger.EXPORT_LIMIT", 1):
            response = self.request("/export", query=PERIOD + "&format=csv")
            self.assertEqual(response.status_code, 422)
