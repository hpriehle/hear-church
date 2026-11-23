"""Hear Church Demo (hearchurch.com/hear-church-demo): a congregation that is always live, for app store reviewers.

It replays a meeting recorded once (demo/, made by scripts/make_demo.py) in a loop: the same captions and translated
voice a real meeting sends, through the same session, hubs and streams, so the iPhone app and the listener page treat
it like any live meeting. Nothing is translated or recognised while it runs, no engine is involved, so nothing is
metered or billed; it has no location, so it is never in a nearby list, only found by search or its link.
On only where HC_DEMO=1 (the public hub): every Mac runs this server too and must not get the demo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..config import ROOT
from .db import Database
from .live import Link, Live, LiveSession

log = logging.getLogger("hear.demo")

DIR = ROOT / "demo"
SLUG, CHURCH, NAME = "hear-church-demo", "Hear Church", "Hear Church Demo"
TICK_S = 0.1


def enabled() -> bool:
    return os.environ.get("HC_DEMO", "").strip() == "1"


def congregation(db: Database) -> dict | None:
    """The demo's row, made on first use: verified, no location, never billed. None if a real one has the slug."""
    c = db.by_slug(SLUG)
    if c is None:
        c = db.create_congregation(church=CHURCH, name=NAME, unit_type="other", email="", free_trial=False)
        db.set_verified(c["id"])
        db.set_billing(c["id"], "internal")
        c = db.congregation(c["id"])
    if c["slug"] != SLUG or c["email"] or c["lat"] is not None:
        log.warning("%s belongs to a real congregation: no demo", SLUG)
        return None
    return c


def start(db: Database, live: Live) -> LiveSession | None:
    """Make the demo live and start replaying it (needs a running event loop)."""
    c = congregation(db)
    if c is None:
        return None
    timeline = json.loads((DIR / "timeline.json").read_text())
    langs = timeline["languages"]
    audio = {code: (DIR / f"{code}.mp3").read_bytes() for code in langs}
    s = LiveSession(c, Link(None, {"id": None, "name": NAME}), None, "demo", langs, langs, live.stream)
    s.link = None   # no engine: a device of this congregation could never take it over
    s.speakers = ["en"]
    live.sessions[(c["id"], s.room)] = s   # the single unnamed room
    s.replay = asyncio.get_running_loop().create_task(_play(live, s, timeline, audio))
    log.info("%s is live (%s), replaying %.0f s", SLUG, ", ".join(langs), timeline["period_s"])
    return s


async def _play(live: Live, s: LiveSession, timeline: dict, audio: dict[str, bytes]) -> None:
    """Captions at their times and each language's MP3 at its own bitrate, from the start again at the end."""
    events, period, rate = timeline["events"], timeline["period_s"], timeline["bitrate_kbps"] * 125
    per_loop = {c: sum(1 for e in events if e["lang"] == c and "text" in e) for c in audio}
    n = i = sent = 0
    start = time.monotonic()
    while live.sessions.get((s.cid, s.room)) is s:   # ended (hidden by the operator): stop
        await asyncio.sleep(TICK_S)
        pos = time.monotonic() - start - n * period
        while i < len(events) and events[i]["t"] <= pos:
            e = events[i]
            seq = n * per_loop[e["lang"]] + e["n"]   # numbers keep rising, so a reconnecting phone gets what it missed
            if "text" in e:
                s.hubs[e["lang"]].publish({"type": "caption", "seq": seq, "session": s.code, "lang": e["lang"],
                                           "text": e["text"], "ts": time.time(), "src": "en"}, remember=True)
            else:
                s.hubs[e["lang"]].publish({"type": "onair", "seq": seq})
            i += 1
        upto = int(min(pos, period) * rate)
        for c, data in audio.items():
            s.streams[c].feed_mp3(data[sent:upto])
        sent = upto
        if pos >= period:
            n, i, sent = n + 1, 0, 0
