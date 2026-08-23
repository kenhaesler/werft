import os
import sys

import pytest

import werft.runner.egress_admin as egress_admin_mod
from werft.runner.egress_admin import (
    BASE_ALLOW,
    allowlist_path,
    clear_allowlist,
    slot_dns_ip,
    slot_from_subnets,
    slot_squid_ip,
    slot_subnet,
    write_allowlist,
)

# --- slot_subnet / slot_squid_ip / slot_dns_ip ---


def test_slot_subnet_formats():
    assert slot_subnet(3) == "10.90.3.0/24"


def test_slot_squid_ip_formats():
    assert slot_squid_ip(3) == "10.90.3.2"


def test_slot_dns_ip_formats():
    assert slot_dns_ip(3) == "10.90.3.3"


def test_slot_subnet_boundary_zero():
    assert slot_subnet(0) == "10.90.0.0/24"


def test_slot_subnet_boundary_255():
    assert slot_subnet(255) == "10.90.255.0/24"


def test_slot_squid_ip_boundary_zero_and_255():
    assert slot_squid_ip(0) == "10.90.0.2"
    assert slot_squid_ip(255) == "10.90.255.2"


def test_slot_dns_ip_boundary_zero_and_255():
    assert slot_dns_ip(0) == "10.90.0.3"
    assert slot_dns_ip(255) == "10.90.255.3"


def test_slot_subnet_custom_prefix():
    assert slot_subnet(7, prefix="10.91") == "10.91.7.0/24"


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_slot_subnet_out_of_range_raises(bad_slot):
    with pytest.raises(ValueError):
        slot_subnet(bad_slot)


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_slot_squid_ip_out_of_range_raises(bad_slot):
    with pytest.raises(ValueError):
        slot_squid_ip(bad_slot)


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_slot_dns_ip_out_of_range_raises(bad_slot):
    with pytest.raises(ValueError):
        slot_dns_ip(bad_slot)


# --- allowlist_path ---


def test_allowlist_path_format():
    assert allowlist_path("/etc/squid/allow", 3) == os.path.join("/etc/squid/allow", "slot3.txt")


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_allowlist_path_out_of_range_raises(bad_slot):
    with pytest.raises(ValueError):
        allowlist_path("/etc/squid/allow", bad_slot)


# --- write_allowlist ---


def test_write_allowlist_content_exact_bytes(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 3, ["Example.com", "Foo.Org"])
    path = os.path.join(allow_dir, "slot3.txt")
    with open(path, "rb") as f:
        content = f.read()
    expected_hosts = sorted(set(BASE_ALLOW) | {"example.com", "foo.org"})
    expected = "".join(f".{h}\n" for h in expected_hosts)
    assert content == expected.encode("utf-8")


def test_write_allowlist_dedupes_and_lowercases(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 1, ["DUP.com", "dup.com", "dup.COM"])
    path = os.path.join(allow_dir, "slot1.txt")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert content.count(".dup.com\n") == 1


def test_write_allowlist_merges_base_allow_with_empty_hosts(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 2, [])
    path = os.path.join(allow_dir, "slot2.txt")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    expected = "".join(f".{h}\n" for h in sorted(BASE_ALLOW))
    assert content == expected


def test_write_allowlist_creates_dir(tmp_path):
    allow_dir = str(tmp_path / "nested" / "allow")
    write_allowlist(allow_dir, 5, ["x.com"])
    assert os.path.isdir(allow_dir)
    assert os.path.isfile(os.path.join(allow_dir, "slot5.txt"))


@pytest.mark.skipif(sys.platform == "win32", reason="posix mode bits")
def test_write_allowlist_dir_mode_0755(tmp_path):
    import stat as stat_mod

    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 1, ["x.com"])
    mode = stat_mod.S_IMODE(os.stat(allow_dir).st_mode)
    assert mode == 0o755


@pytest.mark.skipif(sys.platform == "win32", reason="posix mode bits")
def test_write_allowlist_file_mode_0644(tmp_path):
    import stat as stat_mod

    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 1, ["x.com"])
    path = os.path.join(allow_dir, "slot1.txt")
    mode = stat_mod.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o644


def test_write_allowlist_no_tmp_remnant_on_success(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 1, ["x.com"])
    path = os.path.join(allow_dir, "slot1.txt")
    assert not os.path.exists(path + ".tmp")


def test_write_allowlist_idempotent_rewrite(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 1, ["x.com"])
    path = os.path.join(allow_dir, "slot1.txt")
    with open(path, "rb") as f:
        first = f.read()
    write_allowlist(allow_dir, 1, ["x.com"])
    with open(path, "rb") as f:
        second = f.read()
    assert first == second


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_write_allowlist_out_of_range_raises(tmp_path, bad_slot):
    allow_dir = str(tmp_path / "allow")
    with pytest.raises(ValueError):
        write_allowlist(allow_dir, bad_slot, ["x.com"])


def test_write_allowlist_directory_already_exists_ok(tmp_path):
    allow_dir = str(tmp_path / "allow")
    os.makedirs(allow_dir, exist_ok=True)
    write_allowlist(allow_dir, 1, ["x.com"])
    assert os.path.isfile(os.path.join(allow_dir, "slot1.txt"))


def test_write_allowlist_failure_leaves_no_tmp_remnant(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(egress_admin_mod.os, "replace", _boom)
    allow_dir = str(tmp_path / "allow")

    with pytest.raises(OSError):
        write_allowlist(allow_dir, 1, ["x.com"])

    assert not os.path.exists(os.path.join(allow_dir, "slot1.txt.tmp"))


# --- clear_allowlist ---


def test_clear_allowlist_writes_empty_file(tmp_path):
    allow_dir = str(tmp_path / "allow")
    write_allowlist(allow_dir, 4, ["x.com"])
    path = os.path.join(allow_dir, "slot4.txt")
    assert os.path.getsize(path) > 0

    clear_allowlist(allow_dir, 4)
    assert os.path.getsize(path) == 0


def test_clear_allowlist_creates_dir_if_missing(tmp_path):
    allow_dir = str(tmp_path / "allow")
    clear_allowlist(allow_dir, 4)
    path = os.path.join(allow_dir, "slot4.txt")
    assert os.path.isfile(path)
    assert os.path.getsize(path) == 0


@pytest.mark.parametrize("bad_slot", [-1, 256])
def test_clear_allowlist_out_of_range_raises(tmp_path, bad_slot):
    allow_dir = str(tmp_path / "allow")
    with pytest.raises(ValueError):
        clear_allowlist(allow_dir, bad_slot)


def test_clear_allowlist_no_tmp_remnant(tmp_path):
    allow_dir = str(tmp_path / "allow")
    clear_allowlist(allow_dir, 4)
    path = os.path.join(allow_dir, "slot4.txt")
    assert not os.path.exists(path + ".tmp")


# --- slot_from_subnets ---


def test_slot_from_subnets_reads_the_slot_back():
    assert slot_from_subnets([slot_subnet(7)]) == 7


def test_slot_from_subnets_ignores_foreign_subnets():
    assert slot_from_subnets(["172.30.0.0/24", "10.90.2.0/24"]) == 2


def test_slot_from_subnets_honours_the_prefix():
    assert slot_from_subnets(["10.90.2.0/24"], prefix="10.91") is None
    assert slot_from_subnets(["10.91.2.0/24"], prefix="10.91") == 2


def test_slot_from_subnets_empty_is_none():
    assert slot_from_subnets([]) is None


@pytest.mark.parametrize(
    "subnet",
    [
        "10.90.2.0/16",  # not a /24
        "10.90.2.1/24",  # not the network address
        "10.90.02.0/24",  # not the canonical form this module writes
        "10.90.256.0/24",  # out of range
        "10.90.x.0/24",
        "nonsense",
        "",
    ],
)
def test_slot_from_subnets_rejects_malformed(subnet):
    assert slot_from_subnets([subnet]) is None


def test_slot_from_subnets_tolerates_non_strings():
    assert slot_from_subnets([None, 5, {"Subnet": "10.90.1.0/24"}, "10.90.1.0/24"]) == 1
