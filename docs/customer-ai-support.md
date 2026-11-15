# Customer AI support

Customer support is mounted once at the customer portal route level, outside
individual page headers, so it stays available across customer page navigation.
Guests and local preview sessions see Help with a sign-in prompt; authenticated
customers receive the chat interface. Authentication pages omit the widget.
Manager, Chef and Admin accounts cannot use these endpoints. The existing
customer authentication dependency enforces account status, verified email and
session revocation on every request and again before delivering an answer.

## Behavior

- The floating Help panel supports English, Malayalam, Hindi, automatic language
  selection, conversation history, new conversations, deletion, stop and retry.
- Cookie-authenticated APIs live under `/api/customer/ai`. The old prototype's
  localhost-only check is replaced by customer authentication. The browser never
  calls Ollama directly.
- Ollama `qwen3:1.7b` classifies natural-language questions into a fixed Pydantic
  schema. Explicit quick actions and clear account questions bypass inference.
  There is at most one model call and one business lookup per turn.
- Account figures and statuses are deterministic cards from customer-owned
  records. They are never written by the model. Querying an absent wallet reports
  zero without creating an account. Payment and refund functions never execute.
- Lookups use short, topic-specific replies instead of appending full FAQ text.
  Cards show key facts first, with references and secondary information under
  More details. Dates, times, amounts and status labels use readable formatting;
  the original values remain in the API response and copied support summary.
  Related help articles remain available in an expandable section.
- Approved restaurant discovery, hours, date exceptions, availability and offers
  reuse current application rules. Availability needs a restaurant, date, time
  and party size. A check does not hold a table or guarantee offer eligibility.
- FAQ retrieval searches a small versioned PostgreSQL knowledge corpus in all
  three languages. This deliberately avoids loading an embedding model on the
  8 GB development machine. No pgvector dependency is required. Published FAQ
  answers and source links remain available when inference is offline.
- The POST SSE endpoint emits preparation status and the final validated answer.
  It does not stream hidden reasoning or provisional financial claims.
- Human escalation offers a visible conversation-summary copy action and an
  optional verified HTTPS support URL. It never sends a message or creates a ticket.

## Database

Apply `venv/bin/alembic upgrade head` to install migration
`0055_customer_ai_support` after `0054_manager_dashboard_indexes`. It creates four
new tables: `customer_ai_conversations`, `customer_ai_turns`,
`customer_ai_messages`, and `customer_ai_knowledge`. Existing business tables are
unchanged. Upgrade seeds the immutable `app/customer_ai_seed_v1.py` FAQ dataset.
Do not edit that seed after deployment; publish later revisions through a new
migration. Review translations and policy changes before publishing them.

Conversation queries always include the authenticated owner. Client message
UUIDs make completed replies idempotent, including reconnect/retry after a lost
response. Generation leases expire after the bounded turn deadline, and normal
cancellation releases them immediately. Message contents cannot be changed while
reusing a client message UUID.

History expires after 30 days by default. Expired conversations are hidden and
purged for the current owner when listing or creating conversations. Users can
delete their own conversations. For deployment-wide retention, schedule a
separate maintenance job using
`venv/bin/python -m app.services.customer_ai_maintenance --purge` (batches of 500).
Omit `--purge` for a dry run. No maintenance scheduler is installed automatically.

Business queries run in a separate short session. PostgreSQL transactions are
read-only with a two-second statement timeout; no database connection is held
while waiting for inference. Chat persistence writes only the new AI tables.

## Configuration

`app/core/config.py` loads the existing environment. `AI_CHAT_ENABLED` and
`OLLAMA_TIMEOUT` are accepted as legacy aliases. Prefer these names:

```dotenv
AI_SUPPORT_ENABLED=true
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:1.7b
OLLAMA_TIMEOUT_SECONDS=90
OLLAMA_NUM_CTX=4096
OLLAMA_NUM_PREDICT=256
OLLAMA_KEEP_ALIVE=5m
AI_SUPPORT_TURN_TIMEOUT_SECONDS=120
AI_SUPPORT_MAX_HISTORY_MESSAGES=6
AI_SUPPORT_RETENTION_DAYS=30
AI_SUPPORT_CONTACT_URL=
```

Development defaults enable authenticated support. Set `AI_SUPPORT_ENABLED=false`
to disable it. An empty support URL displays summary copying without inventing a
contact address. Configure only a verified HTTPS destination.

The installed model must be pulled separately with `ollama pull qwen3:1.7b` if it
is not already present. The application never installs or pulls models. Requests
set `think=false`, `num_gpu=0`, bounded context and bounded output. Confirm
`ollama ps` reports CPU loading. The conservative UTF-8 prompt budget trims old
history; oversized requests fall back to deterministic routing and FAQ retrieval.

For the Ollama service on this machine, use `OLLAMA_NUM_PARALLEL=1`,
`OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_MAX_QUEUE=2`, and `OLLAMA_NO_CLOUD=1`.
These are daemon environment settings, distinct from FastAPI configuration.

For a remote inference server, use HTTPS, private networking, firewall limits,
and an authenticated reverse proxy. `OLLAMA_API_KEY` adds a server-only Bearer
header; `OLLAMA_CA_BUNDLE` can select a private CA. TLS verification is never
disabled, redirects are not followed and proxy environment variables are ignored.
Production rejects remote plaintext HTTP inference origins. No infrastructure
settings or credentials are exposed to the widget.

## Limits and operations

- 1 active turn per API process and per conversation; 8 new turns per customer
  per minute; 20 AI POST requests per IP per minute.
- 20 saved conversations per customer, 50 turns per conversation, 1,000 characters
  / 3,000 UTF-8 bytes per message, bounded result lists and response bodies.
- One API worker is recommended for the initial CPU deployment. The per-process
  gate is not a distributed quota: keep Ollama concurrency and queue limits in
  place across workers, and introduce shared admission control before scaling.
- If migrations are missing, APIs return a safe storage-unavailable error rather
  than creating tables at application startup.
- The browser aborts requests on Stop, account change or unmount. Minimizing or
  closing the panel preserves drafts, scroll position and a pending reply. A dropped
  stream can be retried with its original UUID to recover a completed answer.
- The authenticated status response includes `reply_timeout_seconds` (the configured
  turn deadline plus five seconds), so the browser respects CPU deployment limits.
  Availability is checked on open; offline inference is shown as limited service,
  with deterministic account/FAQ lookups still available. The chat clear button
  deletes the current conversation directly. Frontend implementation and the isolated browser fixture
  are documented in `../../DineBook/docs/customer-ai-ui.md`.
- Password/OTP assignments, access tokens and contiguous card-number-shaped input
  are rejected. Users should still avoid entering sensitive information: pattern
  filtering cannot recognize every possible secret format.
- Markdown rendering supports paragraphs, bullet/numbered lists, bold and inline code as React
  text. Raw HTML, model-provided external links and remote images are not rendered.

## Validation

```bash
venv/bin/python -m unittest tests.test_customer_ai tests.test_ollama_service -q
```

From the frontend directory:

```bash
npm run test:customer-ai
npm run build
```

Manual check: sign in as a customer, open Help, ask for wallet and reservations,
switch language, reload history, stop and retry a reply, then delete a conversation.
Confirm that guest/demo sessions show the Help sign-in prompt without calling
chat APIs, and Manager, Chef and Admin pages have no customer widget.
Test another customer's resource UUID and verify no private record is returned.

Native-speaker review and a representative multilingual benchmark remain
necessary before claiming production language quality. Measure cold/warm
first-answer time, total latency, full Ollama service/runner RAM, swap and CPU.
FAQ matching is lexical rather than semantic; unrecognized questions ask for
clarification instead of inventing facts. Financial cards describe recorded
application state, not an independent bank/provider inquiry.
