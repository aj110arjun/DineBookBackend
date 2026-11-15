import asyncio
import json
import unittest
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.db.database import get_db
from app.models.restaurant import RestaurantStatus
from app.models.user import UserRole
from app.models.wallet import WalletAccount
from app.middleware import rate_limit_for
from app.routers.manager.manager_ai import router, ManagerContext, manager_ai_chat
from app.schemas.manager_ai import ManagerAIRequest
from app.services.ai_support_runtime import turn_slots
from app.services.manager_ai_support import read_answer, simple_plan
from app.services.ollama_service import OllamaServiceError
from tests.test_manager_dashboard import DashboardFixtures, NOW


class ManagerAITests(DashboardFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(router)
        self.chef = self.user("Chef", UserRole.CHEF)
        self.admin = self.user("Admin", UserRole.ADMIN)
        self.own_order = self.order(status="PREPARING", payment="PENDING")
        self.foreign_order = self.order(restaurant=self.other_restaurant, total="98765", status="PREPARING", payment="PENDING")
        self.own_visit = self.visit(day=date(2026, 11, 14))
        self.foreign_visit = self.visit(restaurant=self.other_restaurant, day=date(2026, 11, 14))
        self.collection(gross="1000", commission="100")
        self.collection(gross="200", commission="20", refunded_at=NOW)
        self.collection(restaurant=self.other_restaurant, gross="98765", commission="9876")
        self.db.add(WalletAccount(user_id=self.manager.id, balance=321))
        self.db.add(WalletAccount(user_id=self.other_manager.id, balance=99999))
        self.db.commit()
        self.users = {"manager": self.manager, "other": self.other_manager, "customer": self.customer, "chef": self.chef, "admin": self.admin}
        self.tokens = {key: create_access_token(str(user.id))[0] for key, user in self.users.items()}

        def database():
            with Session(self.engine) as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        for target, kwargs in [
            ("app.services.ollama_service.ollama_service.available", {"new_callable": AsyncMock, "return_value": False}),
            ("app.services.ollama_service.ollama_service.chat", {"new_callable": AsyncMock, "side_effect": OllamaServiceError("offline")}),
            ("app.services.manager_ai_support.read_answer", {"side_effect": lambda *args: read_answer(*args, now=NOW)}),
        ]:
            guard = patch(target, **kwargs); mock = guard.start(); self.addCleanup(guard.stop)
            if target.endswith(".chat"): self.model = mock
        guard = patch.object(settings, "ai_support_enabled", True); guard.start(); self.addCleanup(guard.stop)

    def request(self, method="POST", path="/chat", payload=None, user="manager"):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                headers = {"cookie": f"dinebook_access_token={self.tokens[user]}"} if user else {}
                return await client.request(method, "/api/manager/ai" + path, json=payload, headers=headers)
        return asyncio.run(run())

    def send(self, action="overview", message="Today's summary", **extra):
        return self.request(payload={"message": message, "action": action, **extra})

    def test_authentication_roles_approval_and_feature_switch(self):
        self.assertEqual(self.request("GET", "/status", user=None).status_code, 401)
        for role in ("customer", "chef", "admin"):
            self.assertEqual(self.request("GET", "/status", user=role).status_code, 403)
            self.assertEqual(self.request(payload={"message": "wallet"}, user=role).status_code, 403)
        for state in (RestaurantStatus.PENDING, RestaurantStatus.SUSPENDED):
            self.restaurant.status = state; self.db.commit()
            self.assertEqual(self.send().status_code, 403)
        self.restaurant.status = RestaurantStatus.APPROVED; self.db.commit()
        with patch.object(settings, "ai_support_enabled", False):
            self.assertEqual(self.send().status_code, 503)

    def test_status_reports_offline_model_but_usable_quick_actions(self):
        response = self.request("GET", "/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["model_available"])
        self.assertEqual(response.json()["restaurant_id"], str(self.restaurant.id))
        self.assertEqual(response.json()["reply_timeout_seconds"], settings.ai_support_turn_timeout_seconds + 5)

    def test_all_quick_actions_are_scoped_and_issue_only_selects(self):
        statements = []
        def record(conn, cursor, statement, *args): statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", record)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record)
        for action in ("overview", "reservations", "orders", "payments", "revenue", "wallet", "tables", "help", "support"):
            response = self.send(action, action)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(str(self.foreign_order.id), response.text)
            self.assertNotIn(str(self.foreign_visit.id), response.text)
            self.assertNotIn("98765", response.text)
            self.assertNotIn("99999", response.text)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements), statements)
        self.model.assert_not_awaited()

    def test_foreign_references_and_client_selected_restaurants_cannot_leak_records(self):
        for action, reference in (("orders", self.foreign_order.id), ("reservations", self.foreign_visit.id)):
            response = self.send(action, f"Show {reference}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["cards"], [])
        self.assertEqual(self.send(restaurant_id=str(self.other_restaurant.id)).status_code, 422)
        self.assertEqual(self.send(context={"action": "orders", "restaurant_id": str(self.other_restaurant.id)}).status_code, 422)

    def test_revenue_matches_collection_ledger_and_wallet_is_separate(self):
        response = self.send("revenue", "Revenue today")
        self.assertEqual(response.status_code, 200, response.text)
        fields = response.json()["cards"][0]["fields"]
        self.assertEqual(fields["Gross collections"], "INR 1200.00")
        self.assertEqual(fields["Refunds"], "INR 200.00")
        self.assertEqual(fields["Revenue after refunds"], "INR 1000.00")
        self.assertEqual(fields["Net revenue"], "INR 900.00")
        self.assertEqual(self.send("wallet", "Wallet balance").json()["cards"][0]["fields"]["Balance"], "INR 321.00")

    def test_follow_up_periods_retain_topic_and_unsupported_windows_ask(self):
        result = self.send(None, "What about yesterday?", context={"action": "revenue", "period": "today"})
        self.assertEqual(result.json()["context"]["action"], "revenue")
        self.assertEqual(result.json()["cards"][0]["fields"]["From"], "2026-11-12")
        for question in ("Revenue tomorrow", "Revenue last month", "Orders last 30 days", "Revenue this week"):
            response = self.send(None, question)
            self.assertEqual(response.json()["cards"], [])
            self.assertIn("Which view", response.json()["reply"])
        response = self.send("tables", "Table occupancy yesterday")
        self.assertEqual(response.json()["cards"], [])
        self.assertIn("Historical table occupancy", response.json()["reply"])

    def test_past_completed_reservations_are_not_filtered_as_upcoming(self):
        completed = self.visit(day=date(2026, 11, 1), status="COMPLETED"); self.db.commit()
        response = self.send(None, "Completed reservations")
        self.assertIn(str(completed.id), response.text)
        self.assertNotIn(str(self.own_visit.id), response.text)

    def test_payment_period_applies_to_orders_and_scheduled_reservations(self):
        old = self.order(at=NOW - timedelta(days=2), status="PLACED", payment="PENDING"); self.db.commit()
        response = self.send("payments", "Pending payments today")
        self.assertIn(str(self.own_order.id), response.text)
        self.assertNotIn(str(old.id), response.text)

    def test_mutations_and_how_to_questions_give_guidance(self):
        response = self.send("orders", f"Cancel order {self.own_order.id}")
        self.assertEqual(response.json()["cards"], [])
        self.assertIn("portal controls", response.json()["reply"])
        self.db.refresh(self.own_order)
        self.assertEqual(self.own_order.status, "PREPARING")
        response = self.send(None, "How do I add a menu item?")
        self.assertEqual(response.json()["sources"][0]["href"], "/manager/menu")

    def test_unknown_questions_use_bounded_classifier_and_offline_help(self):
        response = self.send(None, "Hello there")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["limited"])
        prompt = json.dumps(self.model.call_args.args[0])
        self.assertNotIn(str(self.own_order.id), prompt)
        self.assertNotIn("321", prompt)
        self.model.side_effect = None
        self.model.return_value = '{"action":"revenue","period":"this_month","state":"all"}'
        response = self.send(None, "How are things going?")
        self.assertIsNone(response.json()["context"]["period"])
        self.assertEqual(response.json()["cards"][0]["fields"]["From"], "2026-11-13")

    def test_input_limits_and_sensitive_values(self):
        for payload in ({"message": " "}, {"message": "x" * 1001}, {"message": "password=hunter2"},
                        {"message": "help", "history": ["x" * 1001]}, {"message": "help", "history": ["hi"] * 7}):
            self.assertEqual(self.request(payload=payload).status_code, 422)

    def test_access_revoked_during_inference_prevents_returning_facts(self):
        async def revoke(*args):
            self.restaurant.status = RestaurantStatus.SUSPENDED; self.db.commit()
            return '{"action":"wallet"}'
        self.model.side_effect = revoke
        self.assertEqual(self.send(None, "Hello there").status_code, 403)

    def test_shared_cpu_gate_and_timeout_release(self):
        from app.routers.customer.customer_ai import _turn_slots
        self.assertIs(_turn_slots, turn_slots)
        self.assertEqual(rate_limit_for("/api/manager/ai/chat", "POST").limit, 20)
        turn_slots.acquire()
        try: self.assertEqual(self.send().status_code, 429)
        finally: turn_slots.release()
        async def delayed(*args): await asyncio.Event().wait()
        with patch.object(settings, "ai_support_turn_timeout_seconds", 0.01), patch("app.routers.manager.manager_ai.answer_manager", side_effect=delayed):
            self.assertEqual(self.send().status_code, 504)
        self.assertEqual(self.send().status_code, 200)

    def test_cancellation_releases_cpu_gate(self):
        async def run():
            started = asyncio.Event()
            async def delayed(*args): started.set(); await asyncio.Event().wait()
            context = ManagerContext(self.manager.id, self.restaurant.id, self.tokens["manager"], self.engine)
            with patch("app.routers.manager.manager_ai.answer_manager", side_effect=delayed):
                task = asyncio.create_task(manager_ai_chat(ManagerAIRequest(message="help"), context))
                await started.wait(); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(run())
        self.assertTrue(turn_slots.acquire(blocking=False)); turn_slots.release()
