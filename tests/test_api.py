from fastapi.testclient import TestClient

from ipamd.app import create_app

AUTH = {"Authorization": "Bearer t0ken", "X-Actor": "great"}


def client(tmp_path, token="t0ken"):
    return TestClient(create_app(str(tmp_path / "api.db"), token=token))


def test_writes_need_token(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/prefixes", json={"cidr": "10.0.0.0/24"}).status_code == 401
    assert c.post("/api/prefixes", json={"cidr": "10.0.0.0/24"}, headers=AUTH).status_code == 201
    assert c.get("/api/prefixes").status_code == 200   # reads are open


def test_flow(tmp_path):
    c = client(tmp_path)
    top = c.post("/api/prefixes", json={"cidr": "10.20.0.0/22", "status": "container", "description": "Lagos"}, headers=AUTH).json()
    sub = c.post(f"/api/prefixes/{top['id']}/allocate", json={"prefix_length": 24, "description": "staff"}, headers=AUTH).json()
    assert sub["cidr"] == "10.20.0.0/24"
    a = c.post("/api/addresses", json={"prefix_id": sub["id"], "hostname": "printer", "skip_first": 10}, headers=AUTH).json()
    assert a["ip"] == "10.20.0.11"
    d = c.get(f"/api/prefixes/{top['id']}").json()
    assert d["free"] == ["10.20.1.0/24", "10.20.2.0/23"]
    assert c.get("/api/search", params={"q": "10.20.0.11"}).json()["addresses"][0]["hostname"] == "printer"
    assert c.get("/api/audit").json()[0]["actor"] == "great"


def test_errors_are_json_with_status(tmp_path):
    c = client(tmp_path)
    r = c.post("/api/prefixes", json={"cidr": "10.0.0.5/24"}, headers=AUTH)
    assert r.status_code == 400 and "did you mean 10.0.0.0/24" in r.json()["detail"]
    c.post("/api/prefixes", json={"cidr": "10.0.0.0/24"}, headers=AUTH)
    assert c.post("/api/prefixes", json={"cidr": "10.0.0.0/24"}, headers=AUTH).status_code == 409
    assert c.get("/api/prefixes/999").status_code == 404


def test_csv_round_trip(tmp_path):
    c = client(tmp_path)
    # children listed before parents on purpose: import must sort them
    csv = "cidr,description\n10.1.1.0/24,voice\n10.1.0.0/16,site b\n10.1.300.0/24,typo\n"
    r = c.post("/api/import/prefixes", content=csv, headers=AUTH).json()
    assert r["added"] == 2 and r["errors"][0]["cidr"] == "10.1.300.0/24"
    rows = c.get("/api/export/prefixes.csv").text.splitlines()
    assert rows[1].startswith("10.1.0.0/16,global") and rows[2].startswith("10.1.1.0/24")


def test_openapi_and_ui(tmp_path):
    c = client(tmp_path, token="")
    assert c.get("/openapi.json").json()["info"]["title"] == "ipamd"
    assert c.get("/").status_code == 200
    assert c.get("/healthz").json()["ok"]
