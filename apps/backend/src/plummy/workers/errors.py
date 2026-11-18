"""Worker error types."""

from __future__ import annotations


class TransientJobError(Exception):
    """A temporary failure (network, provider rate limit, lock held): the task is retried with backoff."""
