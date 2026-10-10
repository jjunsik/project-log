import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from project_log.api import create_app
from project_log.collections import Collections, Memo
from project_log.folders import Folders
from project_log.git import CollectionError
from project_log.native_picker import SCRIPT, NativePicker
from project_log.service import Projects, Registration


def test_native_picker_fixed_command_cancel_and_path_replacement(db, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    selected = tmp_path / "outside folder"
    selected.mkdir()
    picker = NativePicker(Folders(db, home))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, (str(selected) + "/\n").encode(), b"")

    monkeypatch.setattr("project_log.native_picker.subprocess.run", run)
    assert picker.choose() == {"cancelled": False, "path": str(selected)}
    assert calls[0][0] == ["/usr/bin/osascript", "-e", SCRIPT]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert "shell" not in calls[0][1]
    picker.authorize(str(selected))
    selected.rename(tmp_path / "old")
    selected.mkdir()
    with pytest.raises(CollectionError) as caught:
        picker.authorize(str(selected))
    assert caught.value.code == "folder_denied"
    monkeypatch.setattr(
        "project_log.native_picker.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"User cancelled. (-128)"),
    )
    assert picker.choose() == {"cancelled": True, "path": None}


def test_native_picker_timeout_and_duplicate_requests(db, tmp_path, monkeypatch):
    picker = NativePicker(Folders(db, tmp_path))
    entered, release = Event(), Event()

    def run(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return subprocess.CompletedProcess(args, 1, b"", b"User cancelled. (-128)")

    monkeypatch.setattr("project_log.native_picker.subprocess.run", run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(picker.choose)
        assert entered.wait(5)
        with pytest.raises(CollectionError) as caught:
            picker.choose()
        assert caught.value.code == "conflict"
        release.set()
        assert future.result()["cancelled"]

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 180)

    monkeypatch.setattr("project_log.native_picker.subprocess.run", timeout)
    with pytest.raises(CollectionError) as caught:
        picker.choose()
    assert caught.value.code == "picker_unavailable"
    assert not picker.lock.locked()


def test_native_api_requires_origin_intent_and_token_without_os_command_input(
    db, tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        NativePicker, "choose", lambda self: calls.append(True) or {"cancelled": True, "path": None}
    )
    with TestClient(create_app(db, start_worker=False, folder_home=tmp_path)) as client:
        token = client.get("/api/folders/picker").json()["token"]
        headers = {"Origin": "http://127.0.0.1:8000", "X-Project-Log-Intent": "choose-folder"}
        for h, body in [
            ({}, {"token": token}),
            ({"Origin": headers["Origin"]}, {"token": token}),
            (headers, {"token": "wrong"}),
            ({**headers, "Origin": "https://outside.invalid"}, {"token": token}),
        ]:
            assert client.post("/api/folders/picker", headers=h, json=body).status_code == 403
        assert (
            client.post(
                "/api/folders/picker",
                headers=headers,
                json={"token": token, "command": "arbitrary"},
            ).status_code
            == 422
        )
        assert not calls
        assert client.post("/api/folders/picker", headers=headers, json={"token": token}).json()[
            "cancelled"
        ]
        assert len(calls) == 1
        assert (
            client.post(
                "/api/folders/picker",
                headers={**headers, "Host": "outside.invalid"},
                json={"token": token},
            ).status_code
            == 400
        )
        assert len(calls) == 1
        assert db.one("SELECT count(*) AS n FROM folder_roots")["n"] == 0


@pytest.mark.parametrize(
    "state",
    [
        "capturing",
        "queued",
        "running",
        "completed",
        "cancel_pending",
        "cleanup_pending",
        "failed",
        "partial",
    ],
)
def test_memo_state_gate_preserves_snapshot_and_evidence(db, git, tmp_path, state):
    (tmp_path / "repo/a").write_text("original")
    git("add", ".")
    git("commit", "-m", "original")
    p = Projects(db).register(
        Registration(name="test", path=str(tmp_path / "repo"), status="new", base_branch="main")
    )
    cid = str(p["collections"][0]["id"])
    db.execute("UPDATE collections SET state=%s WHERE id=%s", (state, cid))
    before = db.detail(cid)
    evidence = db.all("SELECT * FROM working_entries ORDER BY id")
    if state in {"capturing", "queued", "running", "completed"}:
        changed = Collections(db).memo(cid, Memo(title="수집 메모", description="optional"))
        assert changed["title"] == "수집 메모"
    else:
        with pytest.raises(CollectionError):
            Collections(db).memo(cid, Memo(title="blocked"))
    after = db.detail(cid)
    for row in (before, after):
        row.pop("title")
        row.pop("description")
    assert before == after
    assert db.all("SELECT * FROM working_entries ORDER BY id") == evidence
