"""Event vocabulary shared by triggers, listener, and UI (SPEC §3.1/§3.2)."""

NOTIFY_CHANNEL = "werft_events"

RUN_EVENT_TYPES = frozenset(
    {"created", "status_changed", "dispatch", "ci_observed", "alert", "cleanup"}
)
PROJECT_EVENT_TYPES = frozenset({"onboarded", "lifecycle_flipped", "protection_applied"})
