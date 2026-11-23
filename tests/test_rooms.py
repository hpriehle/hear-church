"""Rooms: one congregation live in several rooms at once, one computer per room (docs/rooms.md)."""
from __future__ import annotations

import asyncio
import json

import websockets

from test_hear import client, engine, hear, listener, make_cong, recv, server, start  # noqa: F401  (fixtures)


async def test_two_computers_run_two_rooms_and_old_apps_get_the_first(server, hear, client):
    cong, token = make_cong(hear)
    other = hear.db.add_device(cong["id"], "Second Mac")
    a, b = await engine(server, token), await engine(server, other)
    first = await start(a, ["fr"])                                  # an older app: no room
    assert first["type"] == "session_started"
    refused = await start(b, ["fr"])                                 # a second unnamed meeting is still refused
    assert refused["type"] == "session_refused" and "already live" in refused["message"]
    assert "room name" in refused["message"]
    second = await start(b, ["es", "fr"], room="  Relief   Society ")
    assert second["type"] == "session_started" and second["session"] != first["session"]

    # a third computer in a room that is already live is refused, whatever the case
    c = await engine(server, hear.db.add_device(cong["id"], "Third Mac"))
    refused = await start(c, ["fr"], room="relief society")
    assert refused["type"] == "session_refused" and "(Relief Society) is already live" in refused["message"]

    info = client.get("/api/hc/c/dakar-ward").json()
    assert [r["room"] for r in info["rooms"]] == ["", "Relief Society"]
    assert [l["code"] for l in info["rooms"][1]["languages"]] == ["es", "fr"]
    assert info["live"] and [l["code"] for l in info["languages"]] == ["fr"]   # old apps: the first room
    assert [r["room"] for r in client.get("/api/hc/search", params={"q": "Dakar"}).json()["results"][0]["rooms"]] \
        == ["", "Relief Society"]

    # streams and the listener socket: no room = the first started; ?room= picks one
    assert client.get("/c/dakar-ward/stream/es.mp3").status_code == 404
    assert client.get("/c/dakar-ward/stream/fr.mp3", params={"room": "Nowhere"}).status_code == 404
    old, init = await listener(server, "dakar-ward", "fr")
    assert init["session"] == first["session"] and init["rooms"] == ["", "Relief Society"]
    ws = await websockets.connect(f"ws://127.0.0.1:{server}/c/dakar-ward/ws/listen?room=Relief%20Society")
    await ws.send(json.dumps({"type": "hello", "lang": "es"}))
    init = await recv(ws, "init")
    assert init["session"] == second["session"] and init["room"] == "Relief Society" and init["lang"] == "es"
    await b.send(json.dumps({"type": "hub", "lang": "es", "remember": True,
                             "msg": {"type": "caption", "text": "Hola", "seq": 1}}))
    assert (await recv(ws, "caption"))["text"] == "Hola"

    # each room meters its own session against the one balance
    for eng, code, lang in ((a, first["session"], "fr"), (b, second["session"], "es")):
        await eng.send(json.dumps({"type": "meter", "session": code, "items": [{"seq": 1, "s": 60.0, "langs": [lang]}]}))
        assert (await recv(eng, "meter_ok"))["seqs"] == [1]
    assert hear.db.speech_by_language(cong["id"]) == {"fr": 60.0, "es": 60.0}

    # the admin sees both rooms
    row = client.get("/api/admin/congregations?key=boss").json()["congregations"][0]
    assert [r["room"] for r in row["rooms"]] == ["", "Relief Society"]

    # the first room ends: phones still there hear the rooms left, and older apps now get the second room
    await a.send(json.dumps({"type": "session_stop"}))
    assert (await recv(ws, "rooms"))["rooms"] == ["Relief Society"]
    assert client.get("/api/hc/c/dakar-ward").json()["languages"][0]["code"] == "es"
    for x in (a, b, c, old, ws):
        await x.close()


async def test_two_rooms_started_in_the_same_second_get_their_own_codes(server, hear, monkeypatch):
    import time
    cong, token = make_cong(hear)
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20261123-100000")
    a, b = await engine(server, token), await engine(server, hear.db.add_device(cong["id"], "Second Mac"))
    codes = [(await start(a, room="Primary"))["session"], (await start(b, room="Nursery"))["session"]]
    assert codes == ["20261123-100000", "20261123-100000-2"]
    await a.close()
    await b.close()


async def test_one_meeting_shows_no_room(server, hear, client):
    cong, token = make_cong(hear)
    a = await engine(server, token)
    await start(a, room="Chapel")
    info = client.get("/api/hc/c/dakar-ward").json()
    assert info["live"] and len(info["rooms"]) == 1                  # a picker needs two
    assert hear.live.get(cong["id"], " chapel ") is hear.live.get(cong["id"])   # ?room= matches as typed, any case
    await a.close()
