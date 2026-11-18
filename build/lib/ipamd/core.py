"""IPAM rules: what nests in what, what's free, what's wrong.

Kept separate from the HTTP layer so the logic can be tested (and used from
a script) without a web server.
"""
from __future__ import annotations

import ipaddress
import re

from .db import DB


class IPAMError(ValueError):
    status = 400


class NotFound(IPAMError):
    status = 404


class Conflict(IPAMError):
    status = 409


def key(addr) -> bytes:
    """16-byte big-endian key. v4 maps into ::ffff:0:0/96 so the two
    families never collide and byte order == numeric order."""
    if addr.version == 4:
        return (0xFFFF00000000 + int(addr)).to_bytes(16, "big")
    return int(addr).to_bytes(16, "big")


def from_key(b: bytes, family: int):
    n = int.from_bytes(b, "big")
    return ipaddress.IPv4Address(n - 0xFFFF00000000) if family == 4 else ipaddress.IPv6Address(n)


MAC = re.compile(r"^[0-9a-f]{2}([:-]?[0-9a-f]{2}){5}$|^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$", re.I)


def norm_mac(m: str) -> str:
    if not m:
        return ""
    if not MAC.match(m.strip()):
        raise IPAMError(f"{m!r} is not a MAC address")
    h = re.sub(r"[^0-9a-f]", "", m.lower())
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


class IPAM:
    def __init__(self, db: DB):
        self.db = db

    # ---- helpers ---------------------------------------------------------

    def vrf_id(self, name: str | None) -> int:
        name = name or "global"
        r = self.db.one("SELECT id FROM vrf WHERE name = ?", (name,))
        if not r:
            raise NotFound(f"no VRF named {name!r}")
        return r["id"]

    def _audit(self, c, actor, action, kind, ref, detail=""):
        c.execute("INSERT INTO audit (actor, action, kind, ref, detail) VALUES (?,?,?,?,?)",
                  (actor, action, kind, ref, detail))

    # ---- VRFs, sites, VLANs ----------------------------------------------------

    def add_vrf(self, name, description="", actor="api"):
        with self.db.tx() as c:
            try:
                cur = c.execute("INSERT INTO vrf (name, description) VALUES (?,?)", (name, description))
            except Exception:
                raise Conflict(f"VRF {name!r} already exists") from None
            self._audit(c, actor, "create", "vrf", name)
        return self.db.one("SELECT * FROM vrf WHERE id = ?", (cur.lastrowid,))

    def add_site(self, name, description="", actor="api"):
        with self.db.tx() as c:
            try:
                cur = c.execute("INSERT INTO site (name, description) VALUES (?,?)", (name, description))
            except Exception:
                raise Conflict(f"site {name!r} already exists") from None
            self._audit(c, actor, "create", "site", name)
        return self.db.one("SELECT * FROM site WHERE id = ?", (cur.lastrowid,))

    def add_vlan(self, vid, name, site_id=None, description="", actor="api"):
        if not 1 <= int(vid) <= 4094:
            raise IPAMError("VLAN ID must be 1-4094")
        with self.db.tx() as c:
            dup = c.execute("SELECT 1 FROM vlan WHERE vid = ? AND site_id IS ?", (vid, site_id)).fetchone()
            if dup:
                raise Conflict(f"VLAN {vid} already exists at that site")
            cur = c.execute("INSERT INTO vlan (vid, name, site_id, description) VALUES (?,?,?,?)",
                            (vid, name, site_id, description))
            self._audit(c, actor, "create", "vlan", f"{vid} {name}")
        return self.db.one("SELECT * FROM vlan WHERE id = ?", (cur.lastrowid,))

    # ---- prefixes --------------------------------------------------------

    def add_prefix(self, cidr, vrf=None, status="active", description="", site_id=None, vlan_id=None, actor="api"):
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=True)
        except ValueError as e:
            # the most common mistake by far: 10.20.1.5/24 instead of 10.20.1.0/24.
            # (careful: IPAMError is a ValueError, so don't raise it inside a
            # try that catches ValueError - it gets swallowed)
            try:
                fixed = ipaddress.ip_network(cidr.strip(), strict=False)
            except ValueError:
                raise IPAMError(str(e)) from None
            raise IPAMError(f"{cidr} has host bits set, did you mean {fixed}?") from None
        vid = self.vrf_id(vrf)
        first, last = key(net.network_address), key(net.broadcast_address)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM prefix WHERE vrf_id = ? AND cidr = ?", (vid, str(net))).fetchone():
                raise Conflict(f"{net} already exists in this VRF")
            # no partial-overlap check needed: two CIDR blocks either nest or
            # don't touch at all, and nesting is exactly what a hierarchy is
            cur = c.execute("""INSERT INTO prefix (vrf_id, family, cidr, first, last, plen, status, description, site_id, vlan_id)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (vid, net.version, str(net), first, last, net.prefixlen, status, description, site_id, vlan_id))
            self._audit(c, actor, "create", "prefix", str(net), description)
        return self.prefix(cur.lastrowid)

    def prefix(self, pid):
        p = self.db.one("""SELECT p.*, v.name AS vrf, s.name AS site, vl.vid AS vlan_vid, vl.name AS vlan_name
                           FROM prefix p JOIN vrf v ON v.id = p.vrf_id
                           LEFT JOIN site s ON s.id = p.site_id LEFT JOIN vlan vl ON vl.id = p.vlan_id
                           WHERE p.id = ?""", (pid,))
        if not p:
            raise NotFound(f"no prefix {pid}")
        return self._decorate(p)

    def _decorate(self, p):
        net = ipaddress.ip_network(p["cidr"])
        p.pop("first", None), p.pop("last", None)
        p["size"] = net.num_addresses
        children = self.db.q("""SELECT cidr FROM prefix WHERE vrf_id = ? AND family = ? AND first >= ? AND last <= ? AND id != ?""",
                             (p["vrf_id"], net.version, key(net.network_address), key(net.broadcast_address), p["id"]))
        child_nets = ipaddress.collapse_addresses(ipaddress.ip_network(c["cidr"]) for c in children)
        child_space = sum(n.num_addresses for n in child_nets)
        used_ips = self.db.one("""SELECT count(*) AS n FROM address WHERE vrf_id = ? AND family = ? AND num BETWEEN ? AND ?""",
                               (p["vrf_id"], net.version, key(net.network_address), key(net.broadcast_address)))["n"]
        usable = net.num_addresses - (2 if net.version == 4 and net.prefixlen < 31 else 0)
        p["children"] = len(children)
        p["addresses"] = used_ips
        # a prefix that's been carved into subnets is "used" by its subnets;
        # a leaf prefix is used by its addresses
        p["utilization"] = round(100 * (child_space / net.num_addresses if children else used_ips / max(1, usable)), 1)
        return p

    def prefixes(self, vrf=None, q=None):
        vid = self.vrf_id(vrf)
        rows = self.db.q("""SELECT p.*, v.name AS vrf, s.name AS site, vl.vid AS vlan_vid, vl.name AS vlan_name
                            FROM prefix p JOIN vrf v ON v.id = p.vrf_id
                            LEFT JOIN site s ON s.id = p.site_id LEFT JOIN vlan vl ON vl.id = p.vlan_id
                            WHERE p.vrf_id = ? ORDER BY p.family, p.first, p.plen""", (vid,))
        out = [self._decorate(r) for r in rows]
        # depth = how many other prefixes contain this one, for the tree view
        nets = [ipaddress.ip_network(r["cidr"]) for r in out]
        for r, n in zip(out, nets):
            r["depth"] = sum(1 for m in nets if m != n and m.version == n.version and n.subnet_of(m))
        if q:
            ql = q.lower()
            out = [r for r in out if ql in r["cidr"] or ql in r["description"].lower() or ql in (r["site"] or "").lower()]
        return out

    def delete_prefix(self, pid, actor="api", force=False):
        p = self.prefix(pid)
        if p["children"] and not force:
            raise Conflict(f"{p['cidr']} has {p['children']} child prefixes; delete those first (or force)")
        with self.db.tx() as c:
            c.execute("DELETE FROM prefix WHERE id = ?", (pid,))
            self._audit(c, actor, "delete", "prefix", p["cidr"])

    def update_prefix(self, pid, actor="api", **fields):
        p = self.prefix(pid)
        allowed = {k: v for k, v in fields.items() if k in ("status", "description", "site_id", "vlan_id") and v is not None}
        if not allowed:
            return p
        with self.db.tx() as c:
            c.execute(f"UPDATE prefix SET {', '.join(k + ' = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), pid))
            self._audit(c, actor, "update", "prefix", p["cidr"], ", ".join(f"{k}={v}" for k, v in allowed.items()))
        return self.prefix(pid)

    def free_blocks(self, pid):
        """The gaps inside a prefix that no child prefix covers, as CIDRs."""
        p = self.prefix(pid)
        net = ipaddress.ip_network(p["cidr"])
        kids = [ipaddress.ip_network(r["cidr"]) for r in self.db.q(
            "SELECT cidr FROM prefix WHERE vrf_id = ? AND family = ? AND first >= ? AND last <= ? AND id != ?",
            (p["vrf_id"], net.version, key(net.network_address), key(net.broadcast_address), pid))]
        free = [net]
        for k in ipaddress.collapse_addresses(kids):
            nxt = []
            for f in free:
                if k.subnet_of(f):
                    nxt.extend(f.address_exclude(k))
                elif not f.subnet_of(k):
                    nxt.append(f)
            free = nxt
        return sorted(free)

    def allocate_prefix(self, pid, plen, description="", status="active", site_id=None, vlan_id=None, actor="api"):
        """Carve the first free /plen out of a parent prefix (smallest
        fitting gap first, to keep big blocks whole)."""
        parent = self.prefix(pid)
        pnet = ipaddress.ip_network(parent["cidr"])
        if plen <= pnet.prefixlen or plen > pnet.max_prefixlen:
            raise IPAMError(f"/{plen} doesn't fit inside {pnet}")
        gaps = [g for g in self.free_blocks(pid) if g.prefixlen <= plen]
        if not gaps:
            raise Conflict(f"no free /{plen} left in {pnet}")
        gaps.sort(key=lambda g: (-g.prefixlen, g.network_address))   # smallest gap that fits, then lowest
        child = next(gaps[0].subnets(new_prefix=plen))
        return self.add_prefix(str(child), vrf=parent["vrf"], status=status, description=description,
                               site_id=site_id, vlan_id=vlan_id, actor=actor)

    # ---- addresses -------------------------------------------------------

    def containing(self, ip, vrf_id):
        """Most specific prefix that contains ip."""
        k = key(ip)
        # family matters: ::/0 would otherwise "contain" every v4 key too
        return self.db.one("""SELECT * FROM prefix WHERE vrf_id = ? AND family = ? AND first <= ? AND last >= ?
                              ORDER BY plen DESC LIMIT 1""", (vrf_id, ip.version, k, k))

    def add_address(self, ip, vrf=None, status="active", hostname="", mac="", description="", actor="api", require_prefix=True):
        try:
            addr = ipaddress.ip_address(ip.strip().split("/")[0])
        except ValueError:
            raise IPAMError(f"{ip!r} is not an IP address") from None
        vid = self.vrf_id(vrf)
        parent = self.containing(addr, vid)
        if require_prefix and not parent:
            raise IPAMError(f"{addr} isn't inside any prefix in this VRF - add the subnet first")
        if parent:
            net = ipaddress.ip_network(parent["cidr"])
            if net.version == 4 and net.prefixlen < 31 and addr in (net.network_address, net.broadcast_address):
                raise IPAMError(f"{addr} is the {'network' if addr == net.network_address else 'broadcast'} address of {net}")
        mac = norm_mac(mac)
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM address WHERE vrf_id = ? AND num = ?", (vid, key(addr))).fetchone():
                raise Conflict(f"{addr} is already assigned")
            cur = c.execute("""INSERT INTO address (vrf_id, family, ip, num, status, hostname, mac, description)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (vid, addr.version, str(addr), key(addr), status, hostname, mac, description))
            self._audit(c, actor, "create", "address", str(addr), hostname)
        return self.address(cur.lastrowid)

    def address(self, aid):
        a = self.db.one("SELECT a.*, v.name AS vrf FROM address a JOIN vrf v ON v.id = a.vrf_id WHERE a.id = ?", (aid,))
        if not a:
            raise NotFound(f"no address {aid}")
        a.pop("num")
        p = self.containing(ipaddress.ip_address(a["ip"]), a["vrf_id"])
        a["prefix"] = p["cidr"] if p else None
        return a

    def addresses(self, pid):
        p = self.prefix(pid)
        net = ipaddress.ip_network(p["cidr"])
        rows = self.db.q("""SELECT a.*, v.name AS vrf FROM address a JOIN vrf v ON v.id = a.vrf_id
                            WHERE a.vrf_id = ? AND a.family = ? AND a.num BETWEEN ? AND ? ORDER BY a.num""",
                         (p["vrf_id"], net.version, key(net.network_address), key(net.broadcast_address)))
        for r in rows:
            r.pop("num")
        return rows

    def next_free_ip(self, pid, skip_first=0):
        """Lowest unassigned host address in a prefix. skip_first reserves
        the bottom of the range (gateway, switches) - common convention."""
        p = self.prefix(pid)
        net = ipaddress.ip_network(p["cidr"])
        hosts = net.hosts() if net.version == 4 else (net.network_address + i for i in range(1, min(net.num_addresses, 1 << 20)))
        taken = {r["ip"] for r in self.addresses(pid)}
        for i, h in enumerate(hosts):
            if i < skip_first:
                continue
            if str(h) not in taken:
                return h
        raise Conflict(f"{net} is full")

    def delete_address(self, aid, actor="api"):
        a = self.address(aid)
        with self.db.tx() as c:
            c.execute("DELETE FROM address WHERE id = ?", (aid,))
            self._audit(c, actor, "delete", "address", a["ip"], a["hostname"])

    # ---- search and audit ------------------------------------------------

    def search(self, q):
        q = q.strip()
        out = {"prefixes": [], "addresses": []}
        try:
            ip = ipaddress.ip_address(q)
        except ValueError:
            ip = None
        if ip:
            for v in self.db.q("SELECT id FROM vrf"):
                p = self.containing(ip, v["id"])
                if p:
                    out["prefixes"].append(self.prefix(p["id"]))
                a = self.db.one("SELECT id FROM address WHERE vrf_id = ? AND num = ?", (v["id"], key(ip)))
                if a:
                    out["addresses"].append(self.address(a["id"]))
            return out
        like = f"%{q.lower()}%"
        for r in self.db.q("SELECT id FROM prefix WHERE lower(cidr) LIKE ? OR lower(description) LIKE ? LIMIT 50", (like, like)):
            out["prefixes"].append(self.prefix(r["id"]))
        for r in self.db.q("""SELECT id FROM address WHERE lower(ip) LIKE ? OR lower(hostname) LIKE ? OR mac LIKE ?
                              OR lower(description) LIKE ? LIMIT 100""", (like, like, like, like)):
            out["addresses"].append(self.address(r["id"]))
        return out

    def health(self):
        """Things a network engineer would want flagged."""
        issues = []
        for r in self.db.q("""SELECT mac, group_concat(ip, ', ') AS ips, count(*) AS n FROM address
                              WHERE mac != '' GROUP BY mac HAVING n > 1"""):
            issues.append({"severity": "medium", "kind": "duplicate-mac",
                           "message": f"MAC {r['mac']} is recorded on {r['n']} addresses: {r['ips']}"})
        for r in self.db.q("""SELECT hostname, group_concat(ip, ', ') AS ips, count(*) AS n FROM address
                              WHERE hostname != '' GROUP BY lower(hostname), family HAVING n > 1"""):
            issues.append({"severity": "low", "kind": "duplicate-hostname",
                           "message": f"hostname {r['hostname']} is on {r['n']} addresses: {r['ips']}"})
        for p in self.db.q("SELECT id FROM prefix WHERE status IN ('active','container')"):
            d = self.prefix(p["id"])
            if d["utilization"] >= 90:
                issues.append({"severity": "high" if d["utilization"] >= 98 else "medium", "kind": "nearly-full",
                               "message": f"{d['cidr']} ({d['description'] or 'no description'}) is {d['utilization']}% used",
                               "prefix_id": d["id"]})
        for a in self.db.q("SELECT id, ip, vrf_id FROM address"):
            if not self.containing(ipaddress.ip_address(a["ip"]), a["vrf_id"]):
                issues.append({"severity": "low", "kind": "orphan-address", "message": f"{a['ip']} isn't inside any prefix"})
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(issues, key=lambda i: order[i["severity"]])

    def audit_log(self, limit=100):
        return self.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
