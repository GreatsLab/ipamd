"""HTTP API + web UI."""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .core import IPAM, IPAMError
from .db import DB

WEB = Path(__file__).parent / "web"


class PrefixIn(BaseModel):
    cidr: str = Field(examples=["10.20.0.0/22"])
    vrf: str = "global"
    status: str = "active"
    description: str = ""
    site_id: int | None = None
    vlan_id: int | None = None


class PrefixPatch(BaseModel):
    status: str | None = None
    description: str | None = None
    site_id: int | None = None
    vlan_id: int | None = None


class AllocateIn(BaseModel):
    prefix_length: int = Field(examples=[26])
    description: str = ""
    status: str = "active"
    site_id: int | None = None
    vlan_id: int | None = None


class AddressIn(BaseModel):
    ip: str | None = Field(None, description="leave empty with prefix_id set to take the next free address")
    prefix_id: int | None = None
    vrf: str = "global"
    status: str = "active"
    hostname: str = ""
    mac: str = ""
    description: str = ""
    skip_first: int = Field(0, description="with prefix_id: keep the first N hosts free (gateways)")


class NamedIn(BaseModel):
    name: str
    description: str = ""


class VlanIn(BaseModel):
    vid: int
    name: str
    site_id: int | None = None
    description: str = ""


def create_app(db_path: str | None = None, token: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("IPAMD_DB", "ipamd.sqlite3")
    token = token if token is not None else os.environ.get("IPAMD_TOKEN", "")
    ipam = IPAM(DB(db_path))
    app = FastAPI(title="ipamd", version=__version__,
                  description="IP address management: prefixes, addresses, VLANs, VRFs. Reads are open, writes need the token if one is set.")

    @app.exception_handler(IPAMError)
    def _err(_: Request, e: IPAMError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    def writer(authorization: str = Header(default=""), x_actor: str = Header(default="")) -> str:
        """Write access. Returns who's acting, for the audit log."""
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "write access needs Authorization: Bearer <token>")
        return x_actor or "api"

    # ---- prefixes ----
    @app.get("/api/prefixes", tags=["prefixes"])
    def list_prefixes(vrf: str = "global", q: str | None = None):
        return ipam.prefixes(vrf, q)

    @app.post("/api/prefixes", status_code=201, tags=["prefixes"])
    def create_prefix(p: PrefixIn, actor: str = Depends(writer)):
        return ipam.add_prefix(p.cidr, p.vrf, p.status, p.description, p.site_id, p.vlan_id, actor=actor)

    @app.get("/api/prefixes/{pid}", tags=["prefixes"])
    def get_prefix(pid: int):
        p = ipam.prefix(pid)
        p["free"] = [str(n) for n in ipam.free_blocks(pid)]
        p["addresses_list"] = ipam.addresses(pid)
        return p

    @app.patch("/api/prefixes/{pid}", tags=["prefixes"])
    def patch_prefix(pid: int, p: PrefixPatch, actor: str = Depends(writer)):
        return ipam.update_prefix(pid, actor=actor, **p.model_dump())

    @app.delete("/api/prefixes/{pid}", status_code=204, tags=["prefixes"])
    def delete_prefix(pid: int, force: bool = False, actor: str = Depends(writer)):
        ipam.delete_prefix(pid, actor=actor, force=force)

    @app.post("/api/prefixes/{pid}/allocate", status_code=201, tags=["prefixes"],
              summary="Carve the next free subnet of a given size out of this prefix")
    def allocate(pid: int, a: AllocateIn, actor: str = Depends(writer)):
        return ipam.allocate_prefix(pid, a.prefix_length, a.description, a.status, a.site_id, a.vlan_id, actor=actor)

    @app.get("/api/prefixes/{pid}/next-ip", tags=["addresses"], summary="Next free address, without taking it")
    def next_ip(pid: int, skip_first: int = 0):
        return {"ip": str(ipam.next_free_ip(pid, skip_first))}

    # ---- addresses ----
    @app.post("/api/addresses", status_code=201, tags=["addresses"])
    def create_address(a: AddressIn, actor: str = Depends(writer)):
        ip, vrf = a.ip, a.vrf
        if not ip:
            if not a.prefix_id:
                raise IPAMError("give an ip, or a prefix_id to take the next free one")
            ip = str(ipam.next_free_ip(a.prefix_id, a.skip_first))
            vrf = ipam.prefix(a.prefix_id)["vrf"]
        return ipam.add_address(ip, vrf, a.status, a.hostname, a.mac, a.description, actor=actor)

    @app.get("/api/addresses/{aid}", tags=["addresses"])
    def get_address(aid: int):
        return ipam.address(aid)

    @app.delete("/api/addresses/{aid}", status_code=204, tags=["addresses"])
    def delete_address(aid: int, actor: str = Depends(writer)):
        ipam.delete_address(aid, actor=actor)

    # ---- the rest ----
    @app.get("/api/search", tags=["search"])
    def search(q: str = Query(min_length=1)):
        return ipam.search(q)

    @app.get("/api/health", tags=["reports"], summary="Nearly full subnets, duplicate MACs, orphan addresses")
    def health():
        return ipam.health()

    @app.get("/api/audit", tags=["reports"])
    def audit(limit: int = 100):
        return ipam.audit_log(min(limit, 1000))

    @app.get("/api/vrfs", tags=["vrfs"])
    def vrfs():
        return ipam.db.q("SELECT * FROM vrf ORDER BY id")

    @app.post("/api/vrfs", status_code=201, tags=["vrfs"])
    def add_vrf(v: NamedIn, actor: str = Depends(writer)):
        return ipam.add_vrf(v.name, v.description, actor=actor)

    @app.get("/api/sites", tags=["sites"])
    def sites():
        return ipam.db.q("SELECT * FROM site ORDER BY name")

    @app.post("/api/sites", status_code=201, tags=["sites"])
    def add_site(s: NamedIn, actor: str = Depends(writer)):
        return ipam.add_site(s.name, s.description, actor=actor)

    @app.get("/api/vlans", tags=["vlans"])
    def vlans():
        return ipam.db.q("SELECT v.*, s.name AS site FROM vlan v LEFT JOIN site s ON s.id = v.site_id ORDER BY v.vid")

    @app.post("/api/vlans", status_code=201, tags=["vlans"])
    def add_vlan(v: VlanIn, actor: str = Depends(writer)):
        return ipam.add_vlan(v.vid, v.name, v.site_id, v.description, actor=actor)

    @app.get("/api/export/prefixes.csv", tags=["reports"], response_class=PlainTextResponse)
    def export_csv(vrf: str = "global"):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["cidr", "vrf", "status", "description", "site", "vlan", "utilization", "addresses"])
        for p in ipam.prefixes(vrf):
            w.writerow([p["cidr"], p["vrf"], p["status"], p["description"], p["site"] or "", p["vlan_vid"] or "",
                        p["utilization"], p["addresses"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="prefixes-{vrf}.csv"'})

    @app.post("/api/import/prefixes", tags=["reports"], summary="CSV with a cidr column (+ optional description, status, vrf)")
    async def import_csv(request: Request, actor: str = Depends(writer)):
        text = (await request.body()).decode("utf-8-sig")
        added, errors = [], []
        rows = list(csv.DictReader(io.StringIO(text)))
        # parents first, so the tree builds in order whatever the file order is
        rows.sort(key=lambda r: int((r.get("cidr") or "0/0").split("/")[-1] or 0))
        for i, r in enumerate(rows, 2):
            try:
                ipam.add_prefix(r["cidr"], r.get("vrf") or "global", r.get("status") or "active", r.get("description") or "", actor=actor)
                added.append(r["cidr"])
            except (IPAMError, KeyError) as e:
                errors.append({"line": i, "cidr": r.get("cidr"), "error": str(e)})
        return {"added": len(added), "errors": errors}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    return app
