import os
import sys

import pytest

from werft.runner.egress import extract_egress_lines

SUBNET = ["172.24.0.0/29"]


def _log(tmp_path, lines):
    p = tmp_path / "access.log"
    p.write_bytes(b"".join(lines))
    return str(p)


def test_matching_lines_survive_and_noise_is_dropped(tmp_path):
    hit = b"1724371200.123    140 172.24.0.3 TCP_TUNNEL/200 4213 CONNECT registry.npmjs.org:443 - HIER_DIRECT/1.2.3.4 -\n"  # noqa: E501
    noise = b"1724371201.456    90 172.24.8.9 TCP_TUNNEL/200 999 CONNECT example.com:443 - HIER_DIRECT/5.6.7.8 -\n"  # noqa: E501
    dest = tmp_path / "out.log"
    n = extract_egress_lines(_log(tmp_path, [noise, hit, noise]), SUBNET, str(dest))
    assert n == len(hit)
    assert dest.read_bytes() == hit


def test_no_match_writes_nothing(tmp_path):
    dest = tmp_path / "out.log"
    assert extract_egress_lines(_log(tmp_path, [b"10.0.0.1 x\n"]), SUBNET, str(dest)) == 0
    assert not dest.exists()


def test_missing_log_and_empty_subnets_skip(tmp_path):
    dest = tmp_path / "out.log"
    assert extract_egress_lines(str(tmp_path / "nope.log"), SUBNET, str(dest)) is None
    assert extract_egress_lines(_log(tmp_path, [b"x\n"]), [], str(dest)) is None
    assert not dest.exists()


def test_tail_window_discards_partial_first_line(tmp_path):
    hit = b"172.24.0.2 recent\n"
    old = b"172.24.0.2 ancient-but-cut-\n"  # will straddle the window edge
    log = _log(tmp_path, [old, hit])
    dest = tmp_path / "out.log"
    n = extract_egress_lines(log, SUBNET, str(dest), max_scan_bytes=len(hit) + 10)
    assert dest.read_bytes() == hit and n == len(hit)


def test_out_cap_keeps_newest(tmp_path):
    lines = [f"172.24.0.4 line-{i:04d}\n".encode() for i in range(100)]
    dest = tmp_path / "out.log"
    cap = sum(len(x) for x in lines[-10:])
    extract_egress_lines(_log(tmp_path, lines), SUBNET, str(dest), max_out_bytes=cap)
    assert dest.read_bytes() == b"".join(lines[-10:])  # front-truncated: oldest dropped


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation")
def test_symlinked_log_is_skipped(tmp_path):
    real = tmp_path / "real.log"
    real.write_bytes(b"172.24.0.2 x\n")
    link = tmp_path / "link.log"
    os.symlink(real, link)
    assert extract_egress_lines(str(link), SUBNET, str(tmp_path / "out.log")) is None


def test_unparseable_subnet_is_skipped_silently(tmp_path):
    hit = b"172.24.0.3 ok\n"
    dest = tmp_path / "out.log"
    n = extract_egress_lines(_log(tmp_path, [hit]), ["not-a-subnet", "172.24.0.0/29"], str(dest))
    assert n == len(hit)
    assert dest.read_bytes() == hit


def test_all_subnets_unparseable_skips(tmp_path):
    dest = tmp_path / "out.log"
    n = extract_egress_lines(_log(tmp_path, [b"172.24.0.3 ok\n"]), ["garbage/64"], str(dest))
    assert n is None
    assert not dest.exists()


def test_ipv6_token_in_subnet_matches(tmp_path):
    hit = b"fd00::3 CONNECT registry.npmjs.org:443\n"
    noise = b"fd01::9 CONNECT example.com:443\n"
    dest = tmp_path / "out.log"
    n = extract_egress_lines(_log(tmp_path, [noise, hit]), ["fd00::/64"], str(dest))
    assert n == len(hit)
    assert dest.read_bytes() == hit


def test_dest_parent_directory_is_created(tmp_path):
    hit = b"172.24.0.3 ok\n"
    dest = tmp_path / "nested" / "dir" / "out.log"
    n = extract_egress_lines(_log(tmp_path, [hit]), SUBNET, str(dest))
    assert n == len(hit)
    assert dest.read_bytes() == hit


def test_empty_log_file_yields_no_match(tmp_path):
    dest = tmp_path / "out.log"
    n = extract_egress_lines(_log(tmp_path, []), SUBNET, str(dest))
    assert n == 0
    assert not dest.exists()


def test_dest_file_mode_is_0644(tmp_path):
    if sys.platform == "win32":
        pytest.skip("posix mode bits")
    import stat as stat_mod

    hit = b"172.24.0.3 ok\n"
    dest = tmp_path / "out.log"
    extract_egress_lines(_log(tmp_path, [hit]), SUBNET, str(dest))
    mode = stat_mod.S_IMODE(os.stat(str(dest)).st_mode)
    assert mode == 0o644
