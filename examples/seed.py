"""Fill a fresh ipamd with a small, realistic example network (an ISP-ish
hosting setup in Lagos plus a branch office) through the API.

    python examples/seed.py http://localhost:8000 [token]
"""
import ipaddress
import json
import random
import sys
import urllib.request

base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
token = sys.argv[2] if len(sys.argv) > 2 else ""
random.seed(3)


def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Actor": "seed",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")


def prefix(cidr, desc, status="active"):
    return call("POST", "/api/prefixes", {"cidr": cidr, "description": desc, "status": status})


def carve(parent, plen, desc, status="active"):
    return call("POST", f"/api/prefixes/{parent['id']}/allocate", {"prefix_length": plen, "description": desc, "status": status})


lagos = prefix("10.20.0.0/16", "Lagos DC", "container")
mgmt = carve(lagos, 24, "management (OOB)")
servers = carve(lagos, 24, "hypervisors + storage")
cust = carve(lagos, 20, "customer VPS pool", "container")
for i in range(3):
    carve(cust, 24, f"VPS pool {i + 1}")
p2p = carve(lagos, 26, "point-to-point links", "container")
for name in ("edge-rtr01 <> core01", "edge-rtr01 <> ISP-A", "edge-rtr02 <> ISP-B"):
    carve(p2p, 31, name)

branch = prefix("10.30.0.0/22", "Abuja branch office", "container")
staff = carve(branch, 24, "staff")
voice = carve(branch, 25, "voice VLAN")
cctv = carve(branch, 27, "CCTV")
prefix("102.89.4.0/24", "public block (from AFRINIC)")
prefix("2c0f:f4c0:1::/48", "public IPv6")

for i, h in enumerate(["core01", "core02", "edge-rtr01", "edge-rtr02", "fw01", "pdu-a", "pdu-b", "ipmi-hv01", "ipmi-hv02", "ipmi-hv03"], 1):
    call("POST", "/api/addresses", {"ip": f"10.20.0.{i}", "hostname": h, "mac": "00:1a:2b:%02x:%02x:%02x" % (0, 0, i), "description": "mgmt"})
for i in range(1, 9):
    call("POST", "/api/addresses", {"ip": f"10.20.1.{10 + i}", "hostname": f"hv{i:02d}", "description": "Proxmox node"})
call("POST", "/api/addresses", {"ip": "10.30.0.1", "hostname": "gw-abuja", "description": "default gateway"})
for i in range(2, 20):
    call("POST", "/api/addresses", {"ip": f"10.30.0.{i}", "status": "reserved", "description": "printers / APs"})
for i in range(100, 200):
    call("POST", "/api/addresses", {"ip": f"10.30.0.{i}", "status": "dhcp", "description": "DHCP pool"})
for i in range(20, 60):
    if random.random() < .7:
        call("POST", "/api/addresses", {"ip": f"10.30.0.{i}", "hostname": f"pc-{i:03d}", "mac": "3c:22:fb:%02x:%02x:%02x" % (1, 0, i)})
for i, h in enumerate(list(ipaddress.ip_network(cctv["cidr"]).hosts())[:26], 1):
    call("POST", "/api/addresses", {"ip": str(h), "hostname": f"cam{i:02d}", "description": "Hikvision"})
# a copy-paste mistake for the health check to find
call("POST", "/api/addresses", {"ip": "10.30.0.60", "hostname": "pc-021", "mac": "3c:22:fb:01:00:15"})
print("seeded")
