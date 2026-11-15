import json
import unittest

import httpx

from app.services.ollama_service import OllamaService, OllamaServiceError


class OllamaServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = OllamaService()
        self.addAsyncCleanup(self.service.close)

    def transport(self, handler):
        self.service.client = httpx.AsyncClient(base_url="http://inference.test", transport=httpx.MockTransport(handler))

    async def test_cpu_and_non_thinking_options_and_bounded_answer(self):
        def respond(request):
            payload = json.loads(request.content)
            self.assertFalse(payload["think"])
            self.assertEqual(payload["options"]["num_gpu"], 0)
            self.assertFalse(payload["stream"])
            return httpx.Response(200, json={"message": {"content": "Safe answer", "thinking": "private reasoning"}})
        self.transport(respond)
        self.assertEqual(await self.service.generate_response("hello"), "Safe answer")

    async def test_invalid_and_reasoning_leaking_answers_are_rejected(self):
        for data in ({"message": {"content": "<think>private</think>answer"}}, {"message": {"content": ""}}, {}, {"message": {"content": "x" * 70000}}):
            self.transport(lambda request: httpx.Response(200, json=data))
            with self.assertRaises(OllamaServiceError):
                await self.service.generate_response("hi")
            await self.service.close()

    async def test_timeout_and_invalid_json_are_safe_errors(self):
        def timeout(request):
            raise httpx.ReadTimeout("internal URL", request=request)
        self.transport(timeout)
        with self.assertRaises(OllamaServiceError):
            await self.service.generate_response("hi")
        await self.service.close()
        self.transport(lambda request: httpx.Response(200, text="invalid"))
        with self.assertRaises(OllamaServiceError):
            await self.service.generate_response("hi")

    async def test_oversized_prompt_is_rejected_before_inference(self):
        def never(request):
            self.fail("Oversized prompt reached inference")
        self.transport(never)
        with self.assertRaises(OllamaServiceError):
            await self.service.generate_response("x" * 30000)
