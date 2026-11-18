import ipaddress

import pytest

from ipamd.core import IPAM, Conflict, IPAMError, NotFound
from ipamd.db import DB


@pytest.fixture
def ipam(tmp_path):
    return IPAM(DB(str(tmp_path / "t.db")))


def test_tree_and_utilization(ipam):
    top = ipam.add_prefix("10.20.0.0/16", description="Lagos office")
    a = ipam.add_prefix("10.20.0.0/24", description="staff")
    ipam.add_prefix("10.20.1.0/24", description="voice")
    tree = {p["cidr"]: p for p in ipam.prefixes()}
    assert tree["10.20.0.0/16"]["depth"] == 0 and tree["10.20.0.0/24"]["depth"] == 1
    # two /24s out of a /16 = 2/256
    assert ipam.prefix(top["id"])["utilization"] == round(100 * 2 / 256, 1)
    for i in range(1, 128):
        ipam.add_address(f"10.20.0.{i}")
    assert ipam.prefix(a["id"])["utilization"] == 50.0


def test_host_bits_hint(ipam):
    with pytest.raises(IPAMError, match="did you mean 10.20.1.0/24"):
        ipam.add_prefix("10.20.1.5/24")


def test_duplicates_and_vrfs(ipam):
    ipam.add_prefix("192.168.1.0/24")
    with pytest.raises(Conflict):
        ipam.add_prefix("192.168.1.0/24")
    # same prefix in another VRF is fine - that's what VRFs are for
    ipam.add_vrf("customer-a")
    ipam.add_prefix("192.168.1.0/24", vrf="customer-a")
    with pytest.raises(NotFound):
        ipam.add_prefix("192.168.2.0/24", vrf="nope")


def test_allocate_next_subnet(ipam):
    top = ipam.add_prefix("10.30.0.0/22", status="container")
    ipam.add_prefix("10.30.0.0/24")
    got = [ipam.allocate_prefix(top["id"], 26)["cidr"] for _ in range(3)]
    assert got == ["10.30.1.0/26", "10.30.1.64/26", "10.30.1.128/26"]
    # a /23 must come from an untouched aligned block, not the fragmented one
    assert ipam.allocate_prefix(top["id"], 23)["cidr"] == "10.30.2.0/23"
    with pytest.raises(Conflict, match="no free /24"):
        ipam.allocate_prefix(top["id"], 24)
    assert [str(n) for n in ipam.free_blocks(top["id"])] == ["10.30.1.192/26"]


def test_smallest_gap_first(ipam):
    top = ipam.add_prefix("10.40.0.0/23")
    ipam.add_prefix("10.40.0.0/25")       # leaves a /25 gap and a whole /24
    assert ipam.allocate_prefix(top["id"], 26)["cidr"] == "10.40.0.128/26"


def test_addresses(ipam):
    p = ipam.add_prefix("10.50.0.0/29")
    with pytest.raises(IPAMError, match="network address"):
        ipam.add_address("10.50.0.0")
    with pytest.raises(IPAMError, match="broadcast address"):
        ipam.add_address("10.50.0.7")
    with pytest.raises(IPAMError, match="isn't inside any prefix"):
        ipam.add_address("172.16.0.1")
    a = ipam.add_address("10.50.0.1", hostname="gw", mac="AA-BB-CC-DD-EE-01")
    assert a["mac"] == "aa:bb:cc:dd:ee:01" and a["prefix"] == "10.50.0.0/29"
    with pytest.raises(Conflict):
        ipam.add_address("10.50.0.1")
    assert str(ipam.next_free_ip(p["id"])) == "10.50.0.2"
    assert str(ipam.next_free_ip(p["id"], skip_first=3)) == "10.50.0.4"
    for i in range(2, 7):
        ipam.add_address(f"10.50.0.{i}")
    with pytest.raises(Conflict, match="full"):
        ipam.next_free_ip(p["id"])


def test_point_to_point_31(ipam):
    p = ipam.add_prefix("10.255.0.0/31")
    ipam.add_address("10.255.0.0")
    ipam.add_address("10.255.0.1")
    assert ipam.prefix(p["id"])["utilization"] == 100.0


def test_ipv6_and_family_separation(ipam):
    ipam.add_prefix("::/0", description="everything v6")
    v4 = ipam.add_prefix("10.60.0.0/24")
    a = ipam.add_address("10.60.0.10")
    # ::/0 must not swallow v4 addresses
    assert a["prefix"] == "10.60.0.0/24"
    assert ipam.prefix(v4["id"])["addresses"] == 1
    p6 = ipam.add_prefix("2c0f:f4c0:1::/48")
    sub = ipam.allocate_prefix(p6["id"], 64)
    assert sub["cidr"] == "2c0f:f4c0:1::/64"
    b = ipam.add_address("2c0f:f4c0:1::1", hostname="router")
    assert b["prefix"] == "2c0f:f4c0:1::/64"


def test_delete_guard(ipam):
    top = ipam.add_prefix("10.70.0.0/16")
    ipam.add_prefix("10.70.1.0/24")
    with pytest.raises(Conflict, match="child prefixes"):
        ipam.delete_prefix(top["id"])
    ipam.delete_prefix(top["id"], force=True)


def test_search(ipam):
    ipam.add_prefix("10.80.0.0/24", description="CCTV cameras")
    ipam.add_address("10.80.0.20", hostname="nvr01", mac="00:11:22:33:44:55")
    r = ipam.search("10.80.0.99")
    assert r["prefixes"][0]["cidr"] == "10.80.0.0/24" and r["addresses"] == []
    assert ipam.search("cctv")["prefixes"][0]["cidr"] == "10.80.0.0/24"
    assert ipam.search("nvr")["addresses"][0]["ip"] == "10.80.0.20"
    assert ipam.search("00:11:22")["addresses"][0]["hostname"] == "nvr01"


def test_health(ipam):
    p = ipam.add_prefix("10.90.0.0/29", description="tiny")
    for i in range(1, 7):
        ipam.add_address(f"10.90.0.{i}", mac="aa:aa:aa:aa:aa:aa" if i < 3 else "")
    kinds = {i["kind"] for i in ipam.health()}
    assert {"nearly-full", "duplicate-mac"} <= kinds


def test_audit(ipam):
    ipam.add_prefix("10.99.0.0/24", actor="great")
    log = ipam.audit_log()
    assert log[0]["actor"] == "great" and log[0]["ref"] == "10.99.0.0/24"
