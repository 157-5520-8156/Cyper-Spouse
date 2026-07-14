"""Shared closed vocabulary for event-driven personal media."""

PRIVACY_LEVELS = frozenset({"ordinary", "personal", "intimate"})
PRIVACY_RANK = {"ordinary": 0, "personal": 1, "intimate": 2}
