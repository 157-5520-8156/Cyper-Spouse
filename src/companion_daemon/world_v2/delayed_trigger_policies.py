"""Versioned retry policies shared by runtime and qualification metadata."""

TECHNICAL_RETRY_POLICY_VERSION = "technical-retry-10-30-120.1"
TECHNICAL_RETRY_BACKOFF_SECONDS = (600, 1_800, 7_200)

__all__ = ["TECHNICAL_RETRY_BACKOFF_SECONDS", "TECHNICAL_RETRY_POLICY_VERSION"]
