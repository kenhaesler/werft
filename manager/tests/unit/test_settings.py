"""Settings defaults and composition (SPEC §10)."""

from werft.config.settings import Settings


def test_t8_settings_defaults():
    s = Settings(database_url="postgresql+asyncpg://x/x")
    assert s.squid_access_log == ""
    assert s.dns_guard_query_log == ""
    assert s.disk_threshold_percent == 90.0
