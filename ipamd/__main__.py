"""python -m ipamd [--host 0.0.0.0] [--port 8000] [--db ipamd.sqlite3]"""
import argparse
import os

import uvicorn


def main():
    ap = argparse.ArgumentParser(prog="ipamd")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=os.environ.get("IPAMD_DB", "ipamd.sqlite3"))
    a = ap.parse_args()
    os.environ["IPAMD_DB"] = a.db
    uvicorn.run("ipamd.app:create_app", factory=True, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
