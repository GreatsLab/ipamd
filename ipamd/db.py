"""SQLite storage. Plain SQL, no ORM - the schema is small and the queries
that matter (containment, next free address) are easier to get right by hand.

Addresses are stored as integers (plus the text form for display) so range
queries are indexable: "every IP inside 10.20.0.0/22" is
`WHERE vrf = ? AND num BETWEEN first AND last`.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS vrf (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS site (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS vlan (
    id          INTEGER PRIMARY KEY,
    vid         INTEGER NOT NULL CHECK (vid BETWEEN 1 AND 4094),
    name        TEXT NOT NULL,
    site_id     INTEGER REFERENCES site(id) ON DELETE SET NULL,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (site_id, vid)
);

CREATE TABLE IF NOT EXISTS prefix (
    id          INTEGER PRIMARY KEY,
    vrf_id      INTEGER NOT NULL REFERENCES vrf(id),
    family      INTEGER NOT NULL,              -- 4 or 6
    cidr        TEXT NOT NULL,
    first       BLOB NOT NULL,                 -- 16-byte big-endian, sorts correctly for v4 and v6
    last        BLOB NOT NULL,
    plen        INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','reserved','deprecated','container')),
    description TEXT NOT NULL DEFAULT '',
    site_id     INTEGER REFERENCES site(id) ON DELETE SET NULL,
    vlan_id     INTEGER REFERENCES vlan(id) ON DELETE SET NULL,
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vrf_id, cidr)
);
CREATE INDEX IF NOT EXISTS prefix_range ON prefix (vrf_id, first, last);

CREATE TABLE IF NOT EXISTS address (
    id          INTEGER PRIMARY KEY,
    vrf_id      INTEGER NOT NULL REFERENCES vrf(id),
    family      INTEGER NOT NULL,
    ip          TEXT NOT NULL,
    num         BLOB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','reserved','dhcp','deprecated')),
    hostname    TEXT NOT NULL DEFAULT '',
    mac         TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vrf_id, num)
);
CREATE INDEX IF NOT EXISTS address_num ON address (vrf_id, num);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,        -- create / update / delete
    kind    TEXT NOT NULL,        -- prefix / address / vlan / ...
    ref     TEXT NOT NULL,        -- what it was about, e.g. 10.0.0.0/24
    detail  TEXT NOT NULL DEFAULT ''
);
"""


class DB:
    """One connection per thread; FastAPI runs sync endpoints in a pool."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        # executescript commits on its own and journal_mode can't change
        # inside a transaction, so the schema goes in before any tx()
        self.conn().executescript(SCHEMA)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO vrf (id, name, description) VALUES (1, 'global', 'default routing table')")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            self._local.c = c
        return c

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None
