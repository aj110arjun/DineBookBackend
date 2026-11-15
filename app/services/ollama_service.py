"""Private, bounded CPU-only Ollama access. No credentials or DB access reach the model."""
import asyncio
import json
import ssl

import httpx

from app.core.config import settings


class OllamaServiceError(Exception):
    pass


class OllamaService:
    def __init__(self):
        self.client = None

    @property
    def model(self):
        return settings.ollama_model

    def http(self):
        if self.client is None or self.client.is_closed:
            try:
                verify = ssl.create_default_context(cafile=settings.ollama_ca_bundle) if settings.ollama_ca_bundle else True
            except (OSError, ssl.SSLError) as exc:
                raise OllamaServiceError('Inference TLS configuration is unavailable') from exc
            self.client = httpx.AsyncClient(
                base_url=settings.ollama_base_url, trust_env=False, follow_redirects=False,
                verify=verify,
                headers={'Authorization': f'Bearer {settings.ollama_api_key.get_secret_value()}'} if settings.ollama_api_key else {},
                timeout=httpx.Timeout(settings.ollama_timeout_seconds, connect=3, pool=2),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            )
        return self.client

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def available(self):
        try:
            response = await self.http().get('/api/tags', timeout=2)
            response.raise_for_status()
            return any(row.get('name') == self.model or row.get('model') == self.model for row in response.json().get('models', []))
        except (httpx.HTTPError, ValueError, AttributeError, TypeError, OllamaServiceError):
            return False

    async def chat(self, messages, schema=None):
        # A UTF-8 byte ceiling is deliberately conservative for this small
        # context. Trim oldest history, never the current user or system message.
        messages = list(messages)
        budget = settings.ollama_num_ctx - settings.ollama_num_predict - 256
        overhead = len(json.dumps(schema or {}).encode())
        while len(messages) > 2 and len(json.dumps(messages, ensure_ascii=False).encode()) + overhead > budget:
            messages.pop(1)
        if len(json.dumps(messages, ensure_ascii=False).encode()) + overhead > budget:
            raise OllamaServiceError('Context budget exceeded')
        payload = {
            'model': self.model, 'messages': messages, 'stream': False, 'think': False,
            'keep_alive': settings.ollama_keep_alive,
            'options': {'temperature': 0.3, 'num_gpu': 0, 'num_ctx': settings.ollama_num_ctx,
                        'num_predict': settings.ollama_num_predict},
        }
        if schema is not None:
            payload['format'] = schema
        try:
            async with asyncio.timeout(settings.ollama_timeout_seconds):
                async with self.http().stream('POST', '/api/chat', json=payload) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 65536:
                            raise OllamaServiceError('Model response limit exceeded')
            data = json.loads(content)
            answer = data['message']['content']
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000 or '<think' in answer.lower() or '</think' in answer.lower():
                raise OllamaServiceError('Invalid model answer')
            return answer.strip()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise OllamaServiceError('Inference is unavailable') from exc

    async def generate_response(self, message: str) -> str:
        return await self.chat([
            {'role': 'system', 'content': 'You are DineBook customer support. Do not invent account facts or claim actions.'},
            {'role': 'user', 'content': message},
        ])


ollama_service = OllamaService()
