"""The money path with rooms (docs/rooms.md, decision 2026-11-22: a congregation's rooms share one balance).
Decision 2026-11-24: translating is billed, phones or not: every language, per speech second."""
from __future__ import annotations

import json

from app.hear import demo
from app.hear import live as hear_live

from test_hear import client, engine, hear, make_cong, recv, server, start  # noqa: F401  (fixtures)
from test_proxy import auth, claude, turn  # noqa: F401  (fixtures)


async def meter(eng, code, minutes, first_seq=0, langs=("fr",)):
    """`minutes` one-minute chunks translated into `langs`, as the Mac reports them (a chunk is at most 60 s)."""
    seqs = list(range(first_seq, first_seq + minutes))
    await eng.send(json.dumps({"type": "meter", "session": code,
                               "items": [{"seq": i, "s": 60.0, "langs": list(langs)} for i in seqs]}))
    assert (await recv(eng, "meter_ok"))["seqs"] == seqs


async def two_rooms(server, hear, cong, token):
    a = await engine(server, token)
    b = await engine(server, hear.db.add_device(cong["id"], "Second Mac"))
    ra, rb = await start(a, ["fr"]), await start(b, ["fr"], room="Relief Society")
    assert ra["type"] == rb["type"] == "session_started"
    return a, b, ra["session"], rb["session"]


async def test_translating_with_no_phone_is_billed_per_language_across_rooms(server, hear, monkeypatch):
    """Riga (riga-2, credits): captions on the Mac, 0 phones. The hub tells every Mac, 0.3.1 ones included (they bill
    only what it calls heard), to bill every language; 2 languages cost twice one; rooms share the balance."""
    monkeypatch.setattr(hear_live, "LISTENERS_EVERY_S", 0.1)
    cong, token = make_cong(hear)
    hear.db.set_free_trial(cong["id"], False)
    a, b, ca, cb = await two_rooms(server, hear, cong, token)
    for x in (a, b):
        m = await recv(x, "listeners")
        assert m["stream"] == m["captions"] == 0 and m["heard"] == ["es", "fr"]   # no phone, both billed
    await meter(a, ca, 20, langs=["es", "fr"])                         # 20 min in 2 languages = 40 language-min
    await meter(a, ca, 20, langs=["fr"])                               # resent with fewer languages: counted once
    await meter(b, cb, 10)                                             # the other room: 10 min in French
    bal = hear.db.balance(cong["id"])
    assert bal["used_min"] == 50 and bal["free_left_min"] == 10 and bal["balance_min"] == 0
    await meter(b, cb, 15, first_seq=10, langs=["es", "fr"])           # 30 more: the free hour, then 20 overdrawn
    assert hear.db.balance(cong["id"])["balance_min"] == -20
    for x in (a, b):
        await x.close()


async def test_two_rooms_meter_against_one_balance_and_one_free_hour(server, hear):
    cong, token = make_cong(hear)
    hear.db.add_credit(cong["id"], "pi_1", 500, seconds=3600)        # one bought hour
    a, b, ca, cb = await two_rooms(server, hear, cong, token)
    await meter(a, ca, 45)
    await meter(b, cb, 45)
    await meter(a, ca, 1, first_seq=0)                                 # a resent chunk counts once

    bal = hear.db.balance(cong["id"])
    # 90 min heard in all: the month's free hour once (not once per room), then 30 min off the one bought hour
    assert bal["used_min"] == 90 and bal["month_used_min"] == 90 and bal["free_left_min"] == 0
    assert bal["balance_min"] == 30 and bal["left_min"] == 30 and bal["can_start"]
    w = hear.db._walk(cong["id"])["sessions"]
    assert sum(s["free"] for s in w.values()) == 3600 and sum(s["paid"] for s in w.values()) == 1800
    for x in (a, b):
        await x.close()


async def test_out_of_credit_stops_every_room_but_never_a_running_one(server, hear, client, claude):
    cong, token = make_cong(hear)
    a, b, ca, cb = await two_rooms(server, hear, cong, token)
    await meter(a, ca, 50)
    await meter(b, cb, 20)                                             # 70 min: 10 min overdrawn, nothing bought
    bal = hear.db.balance(cong["id"])
    assert bal["balance_min"] == -10 and not bal["can_start"]

    # both rooms run on: still metered, and their Macs still translate
    await meter(a, ca, 5, first_seq=50)
    await meter(b, cb, 5, first_seq=20)
    assert hear.db.balance(cong["id"])["balance_min"] == -20
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200

    # a third room is refused like a meeting was before rooms; so is its Mac's translation
    third = hear.db.add_device(cong["id"], "Third Mac")
    c = await engine(server, third)
    for room in ("Primary", "nursery"):
        refused = await start(c, ["fr"], room=room)
        assert refused["type"] == "session_refused" and "is out of credit (balance -$1.67)" in refused["message"]
    r = client.post("/api/hc/translate", json=turn(), headers=auth(third))
    assert r.status_code == 402 and r.json()["error"] == "out_of_credit"

    # a room that ends can't start again (another Mac taking it over neither), the other room goes on
    await a.send(json.dumps({"type": "session_stop"}))
    await meter(a, ca, 1, first_seq=55)                                # its late chunks still count
    assert (await start(a, ["fr"]))["type"] == "session_refused"
    assert (await start(c, ["fr"]))["type"] == "session_refused"
    await meter(b, cb, 1, first_seq=25)
    assert hear.db.balance(cong["id"])["balance_min"] == -22

    # a top-up lets the next room start
    hear.db.add_credit(cong["id"], "pi_2", 500, seconds=3600)
    assert (await start(c, ["fr"], room="Primary"))["type"] == "session_started"
    for x in (a, b, c):
        await x.close()


async def test_internal_and_own_key_rooms_are_never_charged(server, hear):
    for mode, name in (("internal", "Riga Branch"), ("own_key", "Paris Ward")):
        cong, token = make_cong(hear, name=name, email=f"{mode}@example.org")
        hear.db.set_billing(cong["id"], mode)
        a, b, ca, cb = await two_rooms(server, hear, cong, token)
        await meter(a, ca, 120)
        await meter(b, cb, 120)                                        # 4 hours, nothing bought
        bal = hear.db.balance(cong["id"])
        assert bal["metered_min"] == 240                               # metered for the stats
        assert bal["used_min"] == 0 and bal["balance_min"] == 0 and bal["free_left_min"] == 60 and bal["can_start"]
        assert all(s["cost_cents"] == 0 for s in hear.db._walk(cong["id"])["sessions"].values())
        c = await engine(server, hear.db.add_device(cong["id"], "Third Mac"))
        assert (await start(c, ["fr"], room="Primary"))["type"] == "session_started"
        for x in (a, b, c):
            await x.close()

    # the demo congregation (HC_DEMO=1) is internal: never billed, never refused
    d = demo.congregation(hear.db)
    assert d["billing"] == "internal" and hear.db.can_start(d)
