"""Hear Church Demo (app/hear/demo.py): always live from a recording, found by search and link only, never billed."""
import asyncio
import json
import socket

import httpx
import uvicorn
import websockets

from app import main
from app.hear import demo
from tests.test_hear import hear, recv  # noqa: F401  (the hear fixture)

TIMELINE = {"languages": ["es", "fr"], "period_s": 0.6, "bitrate_kbps": 1,   # 1 kbps = 125 bytes a second
            "events": [{"t": 0.05, "lang": "es", "n": 1, "text": "Hola"}, {"t": 0.05, "lang": "fr", "n": 1, "text": "Bonjour"},
                       {"t": 0.1, "lang": "es", "n": 1}]}


def test_the_recorded_meeting_is_complete():
    t = json.loads((demo.DIR / "timeline.json").read_text())
    assert len(t["languages"]) >= 2 and "en" not in t["languages"]
    assert all((demo.DIR / f"{c}.mp3").stat().st_size > t["period_s"] * t["bitrate_kbps"] * 100 for c in t["languages"])
    assert [e["t"] for e in t["events"]] == sorted(e["t"] for e in t["events"]) and t["events"][-1]["t"] < t["period_s"]
    assert all(e["lang"] in t["languages"] for e in t["events"])


def test_only_where_switched_on(monkeypatch):
    monkeypatch.delenv("HC_DEMO", raising=False)
    assert not demo.enabled()
    monkeypatch.setenv("HC_DEMO", "1")
    assert demo.enabled()


async def test_demo_is_live_by_search_and_link_only(hear, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / "timeline.json").write_text(json.dumps(TIMELINE))
    (tmp_path / "es.mp3").write_bytes(b"E" * 75)
    (tmp_path / "fr.mp3").write_bytes(b"F" * 75)
    monkeypatch.setattr(demo, "DIR", tmp_path)
    s = demo.start(hear.db, hear.live)
    c = hear.db.by_slug("hear-church-demo")
    assert c["billing"] == "internal" and not c["free_trial"] and c["email_verified"] and c["lat"] is None
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as http:
            found = (await http.get(f"{base}/api/hc/search", params={"q": "Hear Church Demo"})).json()["results"]
            assert [(r["slug"], r["live"], r["live_english"]) for r in found] == [("hear-church-demo", True, False)]
            assert [l["code"] for l in found[0]["languages"]] == ["es", "fr"]
            assert (await http.get(f"{base}/api/live", params={"lat": 0, "lng": 0})).json()["results"] == []
            assert (await http.get(f"{base}/api/hc/c/hear-church-demo")).json()["live"] is True
            assert (await http.get(f"{base}/hear-church-demo")).status_code == 200
            async with http.stream("GET", f"{base}/c/hear-church-demo/stream/fr.mp3") as r:
                assert r.status_code == 200
                assert set(await asyncio.wait_for(r.aiter_bytes().__anext__(), 2)) == {ord("F")}
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/c/hear-church-demo/ws/listen")
        await ws.send(json.dumps({"type": "hello", "lang": "es"}))
        init = await recv(ws, "init")
        assert init["live"] and init["lang"] == "es" and init["speakers"] == ["en"]
        caps = [await recv(ws, "caption") for _ in range(2)]   # the loop goes round: the numbers keep rising
        assert [m["text"] for m in caps] == ["Hola", "Hola"] and caps[1]["seq"] > caps[0]["seq"]
        assert caps[0]["session"] == init["session"] == "demo"
        await ws.close()
        assert hear.db.sessions(c["id"]) == []   # no meeting recorded, so nothing metered
        assert demo.start(hear.db, hear.live).cid == s.cid   # a restart finds the same congregation
    finally:
        srv.should_exit = True
        await task
        for x in list(hear.live.sessions.values()):
            hear.live.end(x)
