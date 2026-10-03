import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Import after DATA_DIR is set; db_path() reads the env at call time.
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/weeks/1/generate", json={"days": 2})
        yield c


def _board_map(client):
    rows = client.get("/api/weeks/1/board").json()["assignments"]
    return {(r["day"], r["task_id"]): r["member_id"] for r in rows}


def _request(client, a=(0, 1), b=(0, 2)):
    r = client.post("/api/weeks/1/swaps", json={
        "a_day": a[0], "a_task": a[1], "b_day": b[0], "b_task": b[1]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_revoke_confirmed_restores_board(client):
    before = _board_map(client)
    sid = _request(client)
    client.post(f"/api/swaps/{sid}/confirm")
    after_confirm = _board_map(client)
    # confirm actually exchanged the two slots (seeded round-robin: m1<->m2)
    assert after_confirm[(0, 1)] == 2 and after_confirm[(0, 2)] == 1
    assert after_confirm != before

    r = client.post(f"/api/swaps/{sid}/revoke")
    assert r.status_code == 200, r.text
    assert _board_map(client) == before

    detail = client.get(f"/api/swaps/{sid}").json()
    assert detail["status"] == "revoked"
    # detail members must read from the restored board: same as pre-confirm
    assert detail["a_current_member"] == before[(0, 1)] == 1
    assert detail["b_current_member"] == before[(0, 2)] == 2
    assert detail["revoke"] is None


def test_pending_swap_cannot_be_revoked(client):
    before = _board_map(client)
    sid = _request(client)
    r = client.post(f"/api/swaps/{sid}/revoke")
    assert r.status_code == 400
    assert r.json()["detail"] == "not_confirmed"
    assert client.get(f"/api/swaps/{sid}").json()["status"] == "pending"
    assert _board_map(client) == before


def test_revoked_swap_cannot_be_revoked_again(client):
    sid = _request(client)
    client.post(f"/api/swaps/{sid}/confirm")
    assert client.post(f"/api/swaps/{sid}/revoke").status_code == 200
    r = client.post(f"/api/swaps/{sid}/revoke")
    assert r.status_code == 400
    assert r.json()["detail"] == "already_revoked"


def test_revoke_conflict_when_third_swap_moved_slot(client):
    # S swaps A(0,1) <-> B(0,2); then T moves A again to (1,1).
    sid_s = _request(client, (0, 1), (0, 2))
    client.post(f"/api/swaps/{sid_s}/confirm")
    sid_t = _request(client, (0, 1), (1, 1))
    client.post(f"/api/swaps/{sid_t}/confirm")

    board_before_failed_revoke = _board_map(client)
    r = client.post(f"/api/swaps/{sid_s}/revoke")
    assert r.status_code == 409
    assert r.json()["detail"] == "revoke_conflict"
    # rejected revocation leaves board and status untouched
    assert _board_map(client) == board_before_failed_revoke
    assert client.get(f"/api/swaps/{sid_s}").json()["status"] == "confirmed"
    # the blocking swap T itself is still reversible (S's slots not touched by reversing T)
    assert client.post(f"/api/swaps/{sid_t}/revoke").status_code == 200
    assert client.post(f"/api/swaps/{sid_s}/revoke").status_code == 200
    assert client.get(f"/api/swaps/{sid_s}").json()["status"] == "revoked"


def test_list_hides_revoked_but_detail_still_open(client):
    sid = _request(client)
    client.post(f"/api/swaps/{sid}/confirm")
    sid_pending = _request(client, (0, 3), (1, 1))
    client.post(f"/api/swaps/{sid}/revoke")

    ids_default = [r["id"] for r in client.get("/api/swaps").json()]
    assert sid not in ids_default
    assert sid_pending in ids_default
    assert all(r["status"] != "revoked" for r in client.get("/api/swaps").json())

    all_rows = client.get("/api/swaps?include_revoked=true").json()
    revoked_row = next(r for r in all_rows if r["id"] == sid)
    assert revoked_row["status"] == "revoked"

    r = client.get(f"/api/swaps/{sid}")  # revoked detail stays openable
    assert r.status_code == 200
    detail = r.json()
    board = _board_map(client)
    assert detail["status"] == "revoked"
    assert detail["a_current_member"] == board[(0, 1)]
    assert detail["b_current_member"] == board[(0, 2)]


def test_detail_reports_revoke_blocker(client):
    sid_s = _request(client, (0, 1), (0, 2))
    client.post(f"/api/swaps/{sid_s}/confirm")
    sid_t = _request(client, (0, 1), (1, 1))
    client.post(f"/api/swaps/{sid_t}/confirm")

    detail = client.get(f"/api/swaps/{sid_s}").json()
    assert detail["status"] == "confirmed"
    assert detail["revoke"] == {"allowed": False, "reason": "revoke_conflict"}
