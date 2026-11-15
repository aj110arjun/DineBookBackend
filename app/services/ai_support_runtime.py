"""Shared per-process admission gate for the CPU-based assistant deployment."""
from threading import BoundedSemaphore

# Customer and Manager requests share one limit. Configure Ollama's daemon-level
# concurrency separately when deploying multiple API workers.
turn_slots = BoundedSemaphore(1)
