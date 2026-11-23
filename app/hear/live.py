"""Live sessions of Hear Church congregations, published by the congregation's own computer.

The in-memory registry: one live session per congregation, each with a caption feed and an MP3 stream
per language. Sessions are also recorded in the database (sessions table).

An engine (the congregation's Mac) connects to /ws/engine with "Authorization: Bearer <device token>"
(the legacy engine token path in app/engine.py is separate and unchanged) and then:
  engine → hub   {"type": "hello", name, languages, stream, busy, app_version, platform}   (languages = every language it can
                     produce; the binary frames index into this list; stream must match this server's)
                 {"type": "session_start", languages, targets?, speakers?, speaker?, passthrough?, trial?, resume?,
                     private?, room?}   targets (V3, else languages) = the ones offered = the listen pills, in that order;
                     speakers = the languages spoken at the microphone; passthrough (V3) = a listener in a speaker
                     language gets the original (never "listen in the room"); resume = the session code from an
                     earlier "session_started" (reconnect within the grace); private = a "this Wi-Fi only" meeting;
                     live_english = the computer sends the room audio to phones on its Wi-Fi (free, never metered);
                     room = the room this computer's meeting is in ("Relief Society"; "" = unnamed, what older apps send)
                 {"type": "languages", languages, targets?, speakers?, live_english?}   (changed during the meeting)
                 {"type": "hub", lang, msg, remember}  (a caption etc. for that language's listeners)
                 binary: one byte = index into hello.languages, then MP3 bytes
                 {"type": "session_stop"}
                 {"type": "meter", session, items: [{seq, s, langs}]}   seconds of speech per chunk, in the
                     languages someone listened to; any session of this congregation, also after it ended
  hub → engine   {"type": "session_started", session, resumed}   {"type": "session_refused", message}
                 {"type": "session_ended", message}   the hub closed the session itself (congregation hidden)
                 {"type": "listeners", stream, captions, heard, langs}   heard = languages with a phone on them now,
                                                                       langs = {code: [stream, captions]}
                 {"type": "meter_ok", session, seqs, unknown}   the items stored (resent ones too); unknown = no
                     such session here, so the engine drops that report
Rooms (docs/rooms.md): a congregation may be live in several rooms at once, one computer per room; a second device
starting in a room that is already live is refused. Each room is its own session (its own code, listeners and meter
against the congregation's one balance). Listeners name a room (?room=); without one they get the first room started,
which is all older apps know. If the engine's link drops the
session stays open for hearchurch.grace_s (60 s) so a reconnect can resume it, then it ends.
Credits: a new meeting of a credit-billed congregation with nothing left is refused here (`_start`, through
Database.can_start); a running meeting, or one resuming after a drop or a hub restart, never is. Resuming after
the grace counts only from the Mac (device token) that started that meeting, within 6 hours.

Private sessions ("this Wi-Fi only" sharing on credits): the phones listen on the Mac itself, but the meeting still
needs the hub for translation on credit (app/hear/proxy.py) and its meter. Such a session is a billing link only: it
is recorded and metered like any other, but `get` (every public route: the listing, search, the congregation page,
the streams and the listener socket) never returns it, nothing is published to waiting phones, captions and audio
from the engine are dropped, and no listener counts are sent back (the Mac counts its own phones and meters only
the languages they hear). `all` (the proxy, the admin) sees both kinds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..broadcast.hub import Hub
from ..broadcast.stream import Mp3Broadcaster
from ..languages import LANGUAGES
from .db import Database

log = logging.getLogger("hear.live")

# What the engine's own pages announce but this server decides itself.
_SKIP_KINDS = {"languages", "lang_state", "session", "status", "clip"}


LISTENERS_EVERY_S = 5.0   # how often a live session tells its engine how many phones listen
METER_MAX_ITEMS = 500     # items taken from one meter message (the engine sends far fewer)


def room_name(v: Any) -> str:
    """A room name as typed, tidied: "  Relief   Society " → "Relief Society" ("" = unnamed)."""
    return " ".join(str(v or "").split())[:40]


def room_key(v: Any) -> str:
    return room_name(v).casefold()


def _targets(m: dict[str, Any]) -> list[str]:
    """The offered languages of a session_start / languages message: V3 engines send targets (the listen pills, in
    order), older ones only languages (the same list under its old name)."""
    t = m.get("targets") if isinstance(m.get("targets"), list) else m.get("languages")
    return list(dict.fromkeys(c for c in (t or []) if isinstance(c, str)))[:20]


async def _close(ws) -> None:
    try:
        await ws.close(code=4403)
    except Exception:
        pass


class Link:
    """One engine connection."""

    def __init__(self, ws, device: dict[str, Any]):
        self.ws, self.device = ws, device
        self.name = device["name"]
        self.languages: list[str] = []
        self.compatible = False
        self.session: "LiveSession | None" = None
        self.out: asyncio.Queue = asyncio.Queue(maxsize=200)

    def send(self, item: dict) -> None:
        try:
            self.out.put_nowait(item)
        except asyncio.QueueFull:
            pass


class LiveSession:
    def __init__(self, cong: dict[str, Any], link: Link, db_id: int, code: str, offered: list[str],
                 all_langs: list[str], stream) -> None:
        self.cid, self.slug, self.name = cong["id"], cong["slug"], cong["name"]
        self.link: Link | None = link
        self.device_id = link.device["id"]
        self.db_id, self.code, self.offered = db_id, code, offered
        self.private = False  # "this Wi-Fi only": a billing link, never listed or reachable by phones here
        self.speaker = "en"   # the speaker's language: the phones in that language are told to listen in the room
        self.speakers: list[str] = []   # V3: every language spoken at the microphone
        self.passthrough = False        # V3: phones in a speaker language get the original, never paused
        self.live_english = False       # the computer streams the room audio on its Wi-Fi: phones may offer it
        self.room = ""                  # the room's name as the operator typed it ("" = unnamed)
        self.started = time.time()
        self.grace = None  # timer handle while the engine is away
        self.hubs = {c: Hub(f"live-{self.slug}-{c}", history=300, queue_size=400) for c in all_langs}
        self.streams = {c: Mp3Broadcaster(stream.sample_rate, stream.bitrate_kbps, stream.burst_s) for c in all_langs}

    def languages_public(self) -> list[dict[str, str]]:
        return [{"code": c, "name": LANGUAGES[c].name if c in LANGUAGES else c} for c in self.offered]

    def listen_state(self) -> dict[str, Any]:
        """What a listener page needs besides the pills: who speaks, and whether the original is passed through."""
        return {"speaker": self.speaker, "speakers": self.speakers or [self.speaker], "passthrough": self.passthrough}

    def room_public(self) -> dict[str, Any]:
        """One live room for the listener APIs (a picker shows them only when two or more are live)."""
        return {"room": self.room, "languages": self.languages_public(), "live_english": self.live_english,
                "starts_at": self.started}

    def listeners(self) -> int:
        return sum(b.listeners for b in self.streams.values())

    def captions(self) -> int:
        return sum(h.count for h in self.hubs.values())

    def heard(self) -> list[str]:
        """Languages someone is listening to or reading right now: only these are billed."""
        return sorted(c for c in self.hubs if self.hubs[c].count or self.streams[c].listeners)

    def by_lang(self) -> dict[str, list[int]]:
        """Phones per language: [listening to the voice, reading captions] (the operator's listeners per language)."""
        return {c: [self.streams[c].listeners if c in self.streams else 0, self.hubs[c].count] for c in self.hubs
                if self.hubs[c].count or (c in self.streams and self.streams[c].listeners)}


class Live:
    def __init__(self, db: Database, stream, grace_s: float = 60.0):
        self.db, self.stream, self.grace_s = db, stream, grace_s
        self.after_end = None   # called with the congregation id when a meeting ends (auto top-up, app/hear/payments.py)
        self.sessions: dict[tuple[int, str], LiveSession] = {}   # (congregation id, room key), in start order
        self.waiting: dict[int, Hub] = {}   # listeners of a congregation that isn't live yet
        self.started = time.time()          # "boot": caption numbers restart when the server does
        self.links: set[Link] = set()       # engine connections now (a revoked device's are closed)

    def all(self, cid: int) -> list[LiveSession]:
        """Every session of the congregation under way, private ones included, first started first."""
        return [s for (c, _), s in self.sessions.items() if c == cid]

    def rooms(self, cid: int) -> list[LiveSession]:
        """The congregation's public live sessions, for listeners and listings (never a private one)."""
        return [s for s in self.all(cid) if not s.private]

    def get(self, cid: int, room: str | None = None) -> LiveSession | None:
        """The public live session in `room`; without a room the first one started (what older apps get)."""
        rooms = self.rooms(cid)
        if room is None:
            return rooms[0] if rooms else None
        return next((s for s in rooms if s.room.casefold() == room_key(room)), None)

    def get_any(self, cid: int) -> LiveSession | None:
        """The first session under way, private ones included (admin)."""
        return next(iter(self.all(cid)), None)

    def waiting_hub(self, cid: int) -> Hub:
        if cid not in self.waiting:
            self.waiting[cid] = Hub(f"waiting-{cid}", queue_size=50)
        return self.waiting[cid]

    # ------------------------------------------------------------ engine connection
    async def serve_device(self, ws, device: dict[str, Any]) -> None:
        await ws.accept()
        link = Link(ws, device)
        self.links.add(link)
        self.db.touch_device(device["id"])
        sender = asyncio.create_task(self._sender(link))
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    self._mp3(link, m["bytes"])
                elif m.get("text"):
                    try:
                        self._message(link, json.loads(m["text"]))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            log.info("device link closed: %s", e)
        finally:
            self.links.discard(link)
            sender.cancel()
            self.db.touch_device(device["id"])
            self.lost(link)

    async def _sender(self, link: Link) -> None:
        """Everything queued for the engine, plus the listener count whenever it changes (the app shows it)."""
        reported = None
        try:
            while True:
                try:
                    item = await asyncio.wait_for(link.out.get(), LISTENERS_EVERY_S)
                except asyncio.TimeoutError:
                    s = link.session
                    count = (s.listeners(), s.captions(), s.heard(), s.by_lang()) if s and not s.private else None
                    if count is None or count == reported:
                        continue
                    reported = count
                    item = {"type": "listeners", "stream": count[0], "captions": count[1], "heard": count[2],
                            "langs": count[3]}
                await link.ws.send_text(json.dumps(item, ensure_ascii=False))
        except Exception:
            pass

    def _message(self, link: Link, m: dict[str, Any]) -> None:
        t = m.get("type")
        if t == "hello":
            theirs, mine = m.get("stream") or {}, self.stream
            link.name = str(m.get("name") or link.device["name"])[:60]
            link.languages = [c for c in (m.get("languages") or []) if isinstance(c, str)][:20]
            version, platform = m.get("app_version"), m.get("platform")
            if isinstance(version, str) or isinstance(platform, str):   # admin stats: installs; the device lists
                self.db.touch_device(link.device["id"], version if isinstance(version, str) else None,
                                     platform if isinstance(platform, str) else None)
            link.compatible = (theirs.get("sample_rate") == mine.sample_rate
                               and theirs.get("bitrate_kbps") == mine.bitrate_kbps)
            if not link.compatible:
                link.send({"type": "session_refused", "message":
                           f"This server streams {mine.sample_rate} Hz / {mine.bitrate_kbps} kbps; update the app."})
        elif t == "session_start":
            self._start(link, m)
        elif t == "languages" and link.session:
            s = link.session
            if self._set_speakers(s, m):
                self._announce_speaker(s)
            self._set_offered(s, [c for c in _targets(m) if c in s.hubs])
            if "live_english" in m:
                s.live_english = bool(m["live_english"])
        elif t == "session_stop" and link.session:
            self.end(link.session, notify=False)
        elif t == "hub" and link.session and not link.session.private:
            self._caption(link.session, m)
        elif t == "meter":
            self._meter(link, m)

    def _start(self, link: Link, m: dict[str, Any]) -> None:
        def refuse(why: str) -> None:
            # Logged: a refused Mac carries on locally and its translation stops, invisible to the hub otherwise
            log.info("meeting refused for device %s (%s): %s", link.device["id"], link.name, why)
            link.send({"type": "session_refused", "message": why})

        if not link.compatible:
            return refuse("Send a hello with this server's stream settings first.")
        cong = self.db.congregation(link.device["congregation_id"])
        if cong is None or cong["hidden"] or not cong["email_verified"]:
            return refuse("This congregation is not active.")
        if (self.db.device(link.device["id"]) or {}).get("revoked_at"):
            return refuse("This Mac was removed from the congregation: connect it again.")
        offered = [c for c in _targets(m) if c in link.languages]
        if not offered:
            return refuse("Choose at least one language.")
        room = room_name(m.get("room"))
        key = (cong["id"], room.casefold())
        old = self.sessions.get(key)
        if old is not None:
            if old.link is link:
                return refuse("This meeting is already started.")
            same = old.device_id == link.device["id"]
            if m.get("resume") == old.code and (old.link is None or same) and old.private == bool(m.get("private")):
                old.speaker = str(m.get("speaker") or old.speaker)[:8]
                self._set_speakers(old, m)
                old.passthrough = bool(m.get("passthrough"))
                old.live_english = bool(m.get("live_english"))
                self._resume(old, link, offered)
                return
            if old.link is None and same:
                self.end(old)  # the same Mac starts a fresh meeting
            else:
                who = old.link.name if old.link else "another Mac"
                return refuse(f"{cong['name']}{f' ({old.room})' if room else ''} is already live (started on {who})."
                              + (" It may be reconnecting: try again in a minute." if old.link is None else
                                 " To run another meeting at the same time, give this one a room name." if not room
                                 else ""))
        # a meeting already under way (its link dropped past the grace, or this server restarted) is never refused,
        # but only the Mac that started it may carry it on
        resuming = bool(m.get("resume")) and self.db.recent_session(cong["id"], str(m["resume"])[:40],
                                                                    link.device["id"])
        if not resuming and not self.db.can_start(cong):
            b = self.db.balance(cong["id"])
            owed = f"-${-b['balance_usd']:.2f}" if b["balance_usd"] < 0 else f"${b['balance_usd']:.2f}"
            return refuse(f"{cong['name']} is out of credit (balance {owed}). Buy credit to start the next meeting.")
        code = base = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while any(x.code == code for x in self.all(cong["id"])):   # two rooms started in the same second
            n += 1
            code = f"{base}-{n}"
        db_id = self.db.start_session(cong["id"], offered, bool(m.get("trial")), code=code, billing=cong["billing"],
                                     device_id=link.device["id"])
        s = LiveSession(cong, link, db_id, code, offered, link.languages, self.stream)
        s.speaker = str(m.get("speaker") or "en")[:8]
        self._set_speakers(s, m)
        s.passthrough = bool(m.get("passthrough"))
        s.live_english = bool(m.get("live_english"))
        s.private = bool(m.get("private"))
        s.room = room
        self.sessions[key] = s
        link.session = s
        link.send({"type": "session_started", "session": code, "resumed": False})
        if s.private:   # nothing to tell phones here: they listen on the Mac
            log.info("%s started a Wi-Fi-only meeting (%s) on %s", cong["slug"], ", ".join(offered), link.name)
            return
        for c, h in s.hubs.items():
            h.publish({"type": "session", "session": code, "live": True})
        self.waiting_hub(cong["id"]).publish({"type": "reload"})
        self._rooms_changed(cong["id"])
        log.info("%s is live (%s) on %s", cong["slug"], ", ".join(offered), link.name)

    def _resume(self, s: LiveSession, link: Link, offered: list[str]) -> None:
        if s.grace:
            s.grace.cancel()
            s.grace = None
        if s.link is not None and s.link is not link:
            s.link.session = None
        s.link, link.session = link, s
        self._set_offered(s, [c for c in offered if c in s.hubs])
        link.send({"type": "session_started", "session": s.code, "resumed": True})
        log.info("%s resumed on %s", s.slug, link.name)

    @staticmethod
    def _set_speakers(s: LiveSession, m: dict[str, Any]) -> bool:
        """Take the V3 speaker list from a session_start / languages message; True when it changed."""
        if not isinstance(m.get("speakers"), list):
            return False
        new = [c[:8] for c in m["speakers"] if isinstance(c, str)][:20]
        changed, s.speakers = new != s.speakers, new
        return changed

    def _rooms_changed(self, cid: int) -> None:
        """Tell every phone of the congregation which rooms are live (a room picker appears at two)."""
        names = [s.room for s in self.rooms(cid)]
        for s in self.rooms(cid):
            for h in s.hubs.values():
                h.publish({"type": "rooms", "rooms": names})

    def _announce_speaker(self, s: LiveSession) -> None:
        if not s.private:
            for h in s.hubs.values():
                h.publish({"type": "speaker", **s.listen_state()})

    def _set_offered(self, s: LiveSession, codes: list[str]) -> None:
        s.offered = codes
        for h in s.hubs.values():
            h.publish({"type": "languages", "languages": s.languages_public()})

    def _caption(self, s: LiveSession, m: dict[str, Any]) -> None:
        hub = s.hubs.get(m.get("lang"))
        msg = m.get("msg")
        if hub is None or not isinstance(msg, dict) or msg.get("type") in _SKIP_KINDS:
            return
        if msg.get("type") == "caption":
            msg = {**msg, "session": s.code}  # phones compare this with the session they joined
        elif msg.get("type") == "speaker":
            s.speaker = str(msg.get("speaker") or "en")[:8]
            if isinstance(msg.get("speakers"), list):
                self._set_speakers(s, msg)
            if "passthrough" in msg:
                s.passthrough = bool(msg["passthrough"])
            msg = {**msg, **s.listen_state()}
        hub.publish(msg, remember=bool(m.get("remember")))

    def _meter(self, link: Link, m: dict[str, Any]) -> None:
        """Store a speech report and acknowledge what was taken; the engine resends the rest later."""
        code, items = str(m.get("session") or "")[:40], m.get("items")
        try:
            seqs = self.db.record_speech(link.device["congregation_id"],
                                         code, items[:METER_MAX_ITEMS] if isinstance(items, list) else [])
        except Exception:
            log.exception("meter report not stored")
            return  # not acknowledged: the engine sends it again
        link.send({"type": "meter_ok", "session": code, "seqs": seqs or [], "unknown": seqs is None})

    def _mp3(self, link: Link, data: bytes) -> None:
        s = link.session
        if s is None or s.private or len(data) < 2 or data[0] >= len(link.languages):
            return
        b = s.streams.get(link.languages[data[0]])
        if b is not None:
            b.feed_mp3(data[1:])

    # ------------------------------------------------------------ ending
    def revoked(self, did: int) -> None:
        """Device `did` was revoked: its meeting ends and its engine links close (its token no longer opens one)."""
        for s in [s for s in self.sessions.values() if s.device_id == did]:
            self.end(s, why="This Mac was removed from the congregation.")
        for link in [l for l in self.links if l.device["id"] == did]:
            asyncio.ensure_future(_close(link.ws))

    def lost(self, link: Link) -> None:
        """The engine's link closed: keep the session open for a while so it can reconnect."""
        s = link.session
        if s is None or s.link is not link:
            return
        s.link = None
        loop = asyncio.get_running_loop()
        s.grace = loop.call_later(self.grace_s, self._grace_over, s)
        log.info("%s: engine link lost, session kept for %.0f s", s.slug, self.grace_s)

    def _grace_over(self, s: LiveSession) -> None:
        if s.link is None:
            self.end(s)

    def end(self, s: LiveSession, notify: bool = True, why: str = "The server ended this session.") -> None:
        """Close a session. notify: tell the engine, if it is connected, so it stops sending (not needed when
        the engine itself asked)."""
        key = (s.cid, s.room.casefold())
        if self.sessions.get(key) is not s:
            return
        del self.sessions[key]
        if s.grace:
            s.grace.cancel()
        if s.link is not None:
            if notify:
                s.link.send({"type": "session_ended", "message": why})
            s.link.session = None
        self.db.end_session(s.db_id)
        for h in s.hubs.values():
            h.publish({"type": "status", "live": False})
        for b in s.streams.values():
            b.close_all()
        self._rooms_changed(s.cid)
        log.info("%s session ended", s.slug)
        if self.after_end is not None:
            try:
                self.after_end(s.cid)
            except Exception:
                log.exception("after a meeting of %s", s.slug)
