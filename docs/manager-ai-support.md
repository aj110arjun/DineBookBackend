# Manager AI support

Manager support is available at `/api/manager/ai/status` and
`/api/manager/ai/chat`. The endpoints require an active, verified manager session
and an approved restaurant. Ownership comes from authentication and is checked
again after inference and before delivering the response.

Fixed read-only operations cover the dashboard summary, revenue ledger, wallet,
orders, upcoming reservations, pending payments and live tables. Existing manager
dashboard calculations supply revenue, net revenue and table state. Result lists
are bounded. Ollama can classify a question into a fixed topic; it receives no
business records, credentials or database access. Dates and state filters are
derived from the request rather than inferred model facts.

No migration or new configuration is required. This feature shares the customer
assistant's Ollama settings, enable flag, support URL and per-process turn gate.
Manager chat POSTs use a separate 20-request-per-IP-per-minute bucket when the
existing middleware rate limiter is enabled. See `customer-ai-support.md` for
Ollama deployment and multi-worker admission control guidance.

Manager conversation history is session-only in the frontend. The API never
persists manager messages or executes business mutations. PostgreSQL lookups use
read-only transactions with a two-second statement timeout. A separate DB session
is opened after classification; no connection is held while awaiting the model.
The overall turn deadline and Stop/cancellation release the shared turn gate.
Explicit questions and quick actions remain available when the model is offline.

Supported periods are today, yesterday, the last seven days and this month in
the restaurant timezone (IST). Unsupported periods request clarification. Table
occupancy is live only. Wallet replies show the current balance and recent
entries. Pending-payment answers represent application records, with at most five
order payments and five reservation fees, rather than a bank inquiry.

Run `venv/bin/python -m unittest tests.test_manager_ai tests.test_customer_ai tests.test_ollama_service -q`.
Frontend behavior and the isolated preview are documented in
`../../DineBook/docs/manager-ai.md`.
