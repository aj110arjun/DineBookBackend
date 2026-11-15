import asyncio
import importlib.util
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests import sqlite_support  # noqa: F401
from app.core.config import settings
from app.core.security import create_access_token
from app.customer_ai_seed_v1 import KNOWLEDGE_V1
from app.db.database import Base, get_db
from app.main import app
from app.models.customer_ai import CustomerAIConversation, CustomerAIMessage, CustomerAITurn, CustomerAIKnowledge
from app.models.orders import FoodOrder
from app.models.reservation import Reservation
from app.models.restaurant import Restaurant, RestaurantStatus
from app.models.user import User, UserRole
from app.models.wallet import WalletAccount, WalletTransaction
from app.routers.customer.customer_ai import _turn_slots
from app.schemas.ai_chat import AIChatRequest, AIPlan
from app.services.customer_ai_conversations import ConversationRepository
from app.services.customer_ai_support import detect_language, simple_plan
from app.services.customer_ai_tools import CustomerReadTools
from app.services.ollama_service import OllamaServiceError

PREFIX = "/api/customer/ai"


class CustomerAITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.sessions() as db:
            self.users = {key: User(name=key, email=f"{key}@example.com", password_hash="unused", role=role)
                for key, role in [("customer", UserRole.CUSTOMER), ("other", UserRole.CUSTOMER), ("manager", UserRole.MANAGER), ("manager2", UserRole.MANAGER), ("chef", UserRole.CHEF), ("admin", UserRole.ADMIN)]}
            db.add_all(self.users.values()); db.flush()
            self.restaurant = Restaurant(manager_id=self.users["manager"].id, name="Good Kitchen", address="Market Road", city="Kochi", cuisine_type="Indian", status=RestaurantStatus.APPROVED)
            self.hidden = Restaurant(manager_id=self.users["manager2"].id, name="Private Kitchen", address="Hidden", city="Kochi", status=RestaurantStatus.SUSPENDED)
            db.add_all([self.restaurant, self.hidden]); db.flush()
            self.reservations, self.orders = {}, {}
            for index, key in enumerate(("customer", "other")):
                self.reservations[key] = Reservation(user_id=self.users[key].id, restaurant_id=self.restaurant.id,
                    reservation_date=date.today() + timedelta(days=3), start_time=time(19), end_time=time(20, 30),
                    number_of_guests=2, fee_amount=100 + index * 899, status="CONFIRMED", payment_status="PAID")
                self.orders[key] = FoodOrder(customer_id=self.users[key].id, restaurant_id=self.restaurant.id, items=[],
                    subtotal=100 + index * 899, total=118 + index * 881, status="PREPARING", payment_status="PAID")
            db.add_all([*self.reservations.values(), *self.orders.values()])
            db.add_all([CustomerAIKnowledge(**dict(row, version="1")) for row in KNOWLEDGE_V1])
            db.commit()
        self.tokens = {key: create_access_token(str(user.id))[0] for key, user in self.users.items()}

        def override():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db)
        for obj, name, value in ((settings, "rate_limit_enabled", False), (settings, "ai_support_enabled", True)):
            guard = patch.object(obj, name, value); guard.start(); self.addCleanup(guard.stop)
        for name, options in (("available", {"return_value": False}), ("chat", {"side_effect": OllamaServiceError("offline")})):
            guard = patch(f"app.services.ollama_service.ollama_service.{name}", new_callable=AsyncMock, **options)
            setattr(self, name, guard.start()); self.addCleanup(guard.stop)

    def request(self, method, path, payload=None, user="customer"):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                headers = {"cookie": f"dinebook_access_token={self.tokens[user]}"} if user else {}
                return await client.request(method, PREFIX + path, json=payload, headers=headers)
        return asyncio.run(run())

    def conversation(self, user="customer"):
        result = self.request("POST", "/conversations", {"language": "auto"}, user)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    def send(self, action="wallet", message="My wallet", conversation=None, **extra):
        conversation = conversation or self.conversation()
        return self.request("POST", f"/conversations/{conversation}/messages", {
            "message": message, "action": action, "client_message_id": str(uuid.uuid4()), **extra,
        })

    def test_authentication_and_all_other_roles_are_rejected(self):
        self.assertEqual(self.request("GET", "/status", user=None).status_code, 401)
        for role in ("manager", "chef", "admin"):
            for method, path, body in (("GET", "/status", None), ("GET", "/conversations", None), ("POST", "/chat", {"message": "help"})):
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.request(method, path, body, role).status_code, 403)
        with self.sessions() as db:
            db.get(User, self.users["customer"].id).is_active = False; db.commit()
        self.assertEqual(self.request("GET", "/status").status_code, 401)

    def test_status_distinguishes_offline_inference_from_available_storage(self):
        response = self.request("GET", "/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["model_available"])
        self.assertEqual(response.json()["max_message_length"], 1000)
        self.assertEqual(response.json()["reply_timeout_seconds"], settings.ai_support_turn_timeout_seconds + 5)

    def test_private_conversations_are_never_accessible_to_another_customer(self):
        conversation = self.conversation("other")
        for method, suffix, payload in (("GET", "", None), ("DELETE", "", None), ("POST", "/messages", {"message": "help"})):
            self.assertEqual(self.request(method, f"/conversations/{conversation}{suffix}", payload).status_code, 404)
        self.assertEqual(self.request("GET", "/conversations").json(), [])

    def test_wallet_lookup_is_read_only_even_when_no_wallet_exists(self):
        result = self.send()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["cards"][0]["fields"]["Balance"], "INR 0.00")
        self.chat.assert_not_awaited()
        with self.sessions() as db:
            self.assertEqual(db.query(WalletAccount).count(), 0)
            self.assertEqual(db.query(WalletTransaction).count(), 0)
            self.assertEqual(db.query(CustomerAIMessage).count(), 2)

    def test_account_lookups_give_short_topic_specific_replies(self):
        for action in ("reservations", "orders", "wallet", "payments"):
            with self.subTest(action=action):
                result = self.send(action, f"My {action}")
                self.assertEqual(result.status_code, 200, result.text)
                answer = result.json()
                self.assertTrue(answer["cards"])
                self.assertLess(len(answer["reply"].split()), 15)
                self.assertNotIn("\n\n", answer["reply"])
                self.assertNotIn("currently recorded", answer["reply"])

    def test_offer_lookup_asks_for_only_the_restaurant_before_showing_offers(self):
        result = self.send("offers", "Restaurant offers")
        self.assertEqual(result.status_code, 200, result.text)
        answer = result.json()
        self.assertIn("Which restaurant?", answer["reply"])
        self.assertNotIn("date", answer["reply"])
        self.assertNotIn("source of truth", answer["reply"])
        self.assertTrue(answer["cards"])

    def test_empty_restaurant_search_does_not_claim_to_have_matches(self):
        result = self.send("restaurants", "Restaurants in Nowhere")
        self.assertEqual(result.status_code, 200, result.text)
        answer = result.json()
        self.assertEqual(answer["cards"], [])
        self.assertIn("couldn't find a match", answer["reply"])

    def test_customer_lookups_cannot_read_another_customers_ids(self):
        for action, key, source in (("reservations", "reservations", "reservation"), ("orders", "orders", "order"), ("payments", "orders", "order")):
            with self.subTest(action=action), self.sessions() as db:
                plan = AIPlan(action=action, resource_id=getattr(self, key)["other"].id, source=source)
                with self.assertRaises(HTTPException) as raised:
                    CustomerReadTools(db, self.users["customer"].id).execute(plan)
                self.assertEqual(raised.exception.status_code, 404)
        result = self.send("orders", "Show my orders")
        self.assertNotIn(str(self.orders["other"].id), result.text)
        self.assertNotIn("999.00", result.text)

    def test_restaurants_and_direct_restaurant_tools_exclude_suspended_restaurants(self):
        result = self.send("restaurants", "Find restaurants")
        self.assertEqual([card["title"] for card in result.json()["cards"]], ["Good Kitchen"])
        result = self.send("hours", "Hours", restaurant_id=str(self.hidden.id))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["cards"], [])
        self.assertNotIn("Private Kitchen", result.text)

    def test_same_message_id_is_idempotent_and_cannot_be_repurposed(self):
        conversation = self.conversation()
        payload = {"message": "My wallet", "action": "wallet", "client_message_id": str(uuid.uuid4())}
        first = self.request("POST", f"/conversations/{conversation}/messages", payload)
        second = self.request("POST", f"/conversations/{conversation}/messages", payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["message_id"], second.json()["message_id"])
        self.assertEqual(self.request("POST", f"/conversations/{conversation}/messages", dict(payload, message="different")).status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerAITurn).count(), 1)
            self.assertEqual(db.query(CustomerAIMessage).count(), 2)

    def test_limits_and_invalid_tool_arguments(self):
        for payload in ({"message": " "}, {"message": "x" * 1001}, {"message": "hi", "user_id": str(self.users["other"].id)},
                        {"message": "hi", "action": "run_sql"}, {"message": "password: secret123"}):
            self.assertEqual(self.request("POST", "/chat", payload).status_code, 422)
        conversation = self.conversation()
        _turn_slots.acquire()
        try:
            self.assertEqual(self.send(conversation=conversation).status_code, 429)
        finally:
            _turn_slots.release()

    def test_faq_retrieval_and_multilingual_offline_fallback(self):
        for language, question in (("en", "How do wallet refunds work?"), ("ml", "വാലറ്റ് റീഫണ്ട് എങ്ങനെ ലഭിക്കും?"), ("hi", "वॉलेट रिफंड कैसे मिलता है?")):
            result = self.send("faq", question, language=language)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["language"], language)
            self.assertTrue(result.json()["sources"])
        result = self.send(None, "Hello, how does my account work?")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["limited"])

    def test_stream_finishes_with_validated_cards_and_no_reasoning(self):
        conversation = self.conversation()
        result = self.request("POST", f"/conversations/{conversation}/messages/stream", {"message": "My wallet", "action": "wallet"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("text/event-stream", result.headers["content-type"])
        self.assertIn("event: done", result.text)
        self.assertNotIn("thinking", result.text)
        self.assertIn("INR 0.00", result.text)

    def test_history_delete_and_retention_are_owner_scoped(self):
        conversation = self.conversation()
        self.send(conversation=conversation)
        self.assertEqual(len(self.request("GET", f"/conversations/{conversation}").json()["messages"]), 2)
        self.assertEqual(self.request("DELETE", f"/conversations/{conversation}").status_code, 204)
        self.assertEqual(self.request("GET", f"/conversations/{conversation}").status_code, 404)
        old = self.conversation()
        with self.sessions() as db:
            db.get(CustomerAIConversation, uuid.UUID(old)).updated_at = datetime.now(timezone.utc) - timedelta(days=31); db.commit()
        self.assertEqual(self.request("GET", "/conversations").json(), [])
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerAIConversation).count(), 0)

    def test_support_never_claims_a_person_was_contacted(self):
        result = self.send("support", "I need a human")
        self.assertTrue(result.json()["escalation"])
        self.assertIn("No person has been contacted", result.json()["reply"])

    def test_read_tools_emit_only_select_statements(self):
        statements = []
        def capture(connection, cursor, statement, params, context, many):
            statements.append(statement.strip().split()[0].upper())
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with self.sessions() as db:
                tools = CustomerReadTools(db, self.users["customer"].id)
                for action in ("wallet", "reservations", "orders", "payments", "refunds", "restaurants"):
                    tools.execute(AIPlan(action=action))
            self.assertTrue(statements)
            self.assertEqual(set(statements), {"SELECT"})
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

    def test_revocation_during_inference_prevents_answer_delivery(self):
        from app.schemas.ai_chat import AIAnswer
        async def revoked(*args):
            with self.sessions() as db:
                db.get(User, self.users["customer"].id).is_active = False; db.commit()
            return AIAnswer(reply="private", language="en", as_of=datetime.now(timezone.utc)), AIPlan()
        with patch("app.routers.customer.customer_ai.answer_customer", side_effect=revoked):
            self.assertEqual(self.send().status_code, 401)
        with self.sessions() as db:
            self.assertEqual(db.query(CustomerAIMessage).filter(CustomerAIMessage.sender == "assistant").count(), 0)
            self.assertIsNone(db.query(CustomerAIConversation).one().lease_token)

    def test_auto_language_preference_survives_history_reload(self):
        conversation = self.conversation()
        self.send("wallet", "എന്റെ വാലറ്റ്", conversation=conversation)
        result = self.request("GET", f"/conversations/{conversation}").json()
        self.assertEqual(result["conversation"]["language"], "auto")
        self.assertEqual(result["messages"][-1]["answer"]["language"], "ml")
        result = self.send("wallet", "Show my wallet", conversation=conversation)
        self.assertEqual(result.json()["language"], "en")

    def test_cancellation_releases_the_generation_lease_and_slot(self):
        from app.routers.customer.customer_ai import run_turn, CustomerContext
        conversation = uuid.UUID(self.conversation())
        async def run():
            started = asyncio.Event()
            async def delayed(*args):
                started.set()
                await asyncio.Event().wait()
            context = CustomerContext(self.users["customer"].id, self.tokens["customer"], self.engine)
            with patch("app.routers.customer.customer_ai.answer_customer", side_effect=delayed):
                task = asyncio.create_task(run_turn(context, conversation, AIChatRequest(message="help")))
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        asyncio.run(run())
        with self.sessions() as db:
            self.assertIsNone(db.get(CustomerAIConversation, conversation).lease_token)
            self.assertEqual(db.query(CustomerAITurn).one().status, "cancelled")
        self.assertEqual(self.send(conversation=str(conversation)).status_code, 200)

    def test_per_customer_message_limit(self):
        conversation = self.conversation()
        for _ in range(8):
            self.assertEqual(self.send(conversation=conversation).status_code, 200)
        self.assertEqual(self.send(conversation=conversation).status_code, 429)


class CustomerAIUtilityTests(unittest.TestCase):
    def test_languages_and_explicit_selection(self):
        for message, expected in (("ente booking confirm aayo?", "ml"), ("mera refund kya hai", "hi"), ("എന്റെ ബുക്കിംഗ്", "ml"), ("ऑर्डर की स्थिति", "hi"), ("What is my order status?", "en")):
            self.assertEqual(detect_language(message), expected)
        self.assertEqual(detect_language("hello", "ml"), "ml")

    def test_relative_dates_and_time_arguments(self):
        plan = simple_plan(AIChatRequest(message="availability tomorrow at 7pm for 4 people"), {})
        self.assertEqual(plan.start, time(19))
        self.assertEqual(plan.guests, 4)
        self.assertIsNotNone(plan.day)
        continuation = simple_plan(AIChatRequest(message="tomorrow at 7pm for 4 people"), {"action": "availability"})
        self.assertEqual(continuation.action, "availability")

    def test_migration_up_down_preserves_business_tables(self):
        path = Path(__file__).parents[1] / "alembic/versions/0055_customer_ai_support.py"
        spec = importlib.util.spec_from_file_location("customer_ai_migration", path)
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE users (id CHAR(32) PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                self.assertEqual(connection.execute(text("SELECT count(*) FROM customer_ai_knowledge")).scalar(), len(KNOWLEDGE_V1))
                migration.downgrade()
                self.assertEqual(inspect(connection).get_table_names(), ["users"])
