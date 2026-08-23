"""Settings defaults and composition (SPEC §10)."""

from werft.config.settings import Settings


def test_t8_settings_defaults():
    s = Settings(database_url="postgresql+asyncpg://x/x")
    assert s.squid_access_log == ""
    assert s.dns_guard_query_log == ""
    assert s.disk_threshold_percent == 90.0


def test_t9_egress_activation_settings_defaults():
    s = Settings(database_url="postgresql+asyncpg://x/x")
    assert s.egress_slot_count == 0
    assert s.egress_subnet_prefix == "10.90"
    assert s.egress_allowlist_dir == "/srv/werft/egress/allow"
    assert s.egress_proxy_container == "werft-egress-proxy"
    assert s.dns_guard_container == "werft-dns-guard"
    assert s.egress_proxy_port == 3128
