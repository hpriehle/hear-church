# Rooms, and phones that keep listening when the server is lost

Decisions of 2026-11-23. Code: `app/hear/live.py` (hub), `app/engine.py` (`PublishClient`), `app/main.py`
(`_may_listen`, `wifi_fallback`), `web/listener/`, the Mac and Windows top bars. Tests: `tests/test_rooms.py`.

## Rooms

A congregation can run several meetings at once, one computer per room.

- **Operator.** Next to Start meeting there is an optional **Room** field ("Relief Society"). The computer remembers
  it (engine setting `server.room`, `POST /api/settings {"server": {"room": "…"}}`). It is sent to the hub in
  `session_start` as `room`. Empty = unnamed, which is what older apps send. The field is hidden with
  "This Wi-Fi only".
- **Hub.** Live sessions are keyed by (congregation, room). The key is the name with spaces tidied and case ignored.
  A second computer in a room that is already live is refused as before. A second *unnamed* meeting gets
  "… is already live (started on X). To run another meeting at the same time, give this one a room name."
  Each room is its own session: its own code, listeners and meter. All rooms use the congregation's one balance,
  `can_start` is checked at each start, and a meeting already under way is never cut off. Two rooms that start in
  the same second get codes `…-100000` and `…-100000-2`.
- **One meeting only:** no room is shown anywhere. Every client shows a room picker only when `rooms` has 2 or more
  entries.
- **Older clients** (Mac 0.3.0, iPhone builds 4/5, old web pages) never send a room. They get the **first room
  started**, through every existing URL.

### Listener API (hub)

All existing fields and URLs are unchanged. What's new:

| Where | New |
|---|---|
| `GET /api/live`, `GET /api/hc/search` (each result) | `rooms: [{room, languages, live_english, starts_at}]`, the live rooms, first started first. `live`, `languages`, `live_english` and `starts_at` describe the first room. |
| `GET /api/hc/c/<slug>` | `rooms`, the same list. |
| `GET /c/<slug>/stream/<lang>.mp3?room=<name>` | `room` is optional. Without it you get the first room. An unknown room or a room that isn't live gives 404. `?room=` (empty) = the unnamed room. |
| `WS /c/<slug>/ws/listen?room=<name>` | `room` is optional, as above. `init` gains `rooms: [names]` (always) and `room` (when live). A room that isn't live waits like a congregation that isn't live (`reload` when any room starts). |
| listener socket message | `{"type": "rooms", "rooms": [names]}` is sent to every phone of the congregation when a room starts or ends. |
| admin | `GET /api/admin/congregations` and `/{id}` gain `rooms: [{room, wifi_only, listeners, session?}]`, and `listeners` adds all rooms up. |

Room names are free text (up to 40 characters). `""` is the unnamed room: clients label it "Main room".

### Web listener (`hearchurch.com/<slug>`)

With 2+ rooms live, a row of room buttons appears above the languages. The current room is highlighted, which is
the first room started until the phone picks one. Picking a room reconnects the captions and the voice with
`?room=`. If the chosen room ends while others go on, the page moves to the first live room.

## Server lost mid-meeting (the computer serves the Wi-Fi by itself)

The app now always listens on `0.0.0.0` (it used to listen on 127.0.0.1 unless "This Wi-Fi only" was on at
launch). From the network, only the listening routes answer: `/`, `/stream/<lang>.mp3`, `/stream.mp3`,
`/clips/…`, `/qr.svg`, `/ws/listen`. They answer only:

- in "This Wi-Fi only" mode, with the meeting key (as before), or
- **while the server is lost during an online meeting**, with no key (the meeting was public online anyway).

Every operator, settings and control route, HTTP or WebSocket, refuses any client that is not this computer, even
with a password (`_role` in app mode, the `app_mode_lan` middleware). `tests/test_rooms.py` checks this.

"Lost" (`PublishClient.lost_server`) means the app is running an online meeting (not Wi-Fi only, not /talk) and its
link to the hub is down. The link counts as down when the socket closes or its pings time out (about 50 s). While
lost, the computer:

1. Shows the operator: "Can't reach Hear Church's server. Phones on this Wi-Fi with the Hear Church app keep
   listening to this computer. The meeting goes on and returns to the server by itself."
2. Announces itself by Bonjour (below), and serves the listening routes to the Wi-Fi.
3. Keeps metering the languages its own listeners hear, under the same session code. The reports go to the hub
   when it is back.

When the link returns, it resumes the same hub session (within the hub's 60 s grace; after that the hub opens a new
session for the same computer without a credit check), stops the announcement, and closes the Wi-Fi. The meeting
itself never stops.

Notes:
- Translation on Hear Church credit goes through the hub. While the hub can't be reached, captions still come
  through in the speaker's language, but nothing is translated until it is back (same as "This Wi-Fi only"
  without internet). Congregations on their own API key keep translating if only hearchurch.com is down.
- Web pages opened from hearchurch.com can't find the computer (browsers have no Bonjour). Only the phone app can
  follow it.
- Windows: the "Allow in Windows Firewall" step for Live now adds one rule, "Hear Church (Live and Wi-Fi)": inbound,
  any protocol, the engine program only. It covers Live's UDP, Bonjour and this TCP fallback. PCs that only have
  the older "Hear Church (Live)" rule are asked once more. A PC that never allowed it may keep phones out.
- Mac: `_hearchurch._tcp` is added to `NSBonjourServices`. Local Network permission is the one Live already asks for.

## For the iPhone app (separate step): the exact contract

### 1. Rooms (online)

- From `GET /api/hc/c/<slug>` (or the `/api/live` / search entry), read `rooms`.
- **If `rooms.length >= 2`:** show a room choice *before* language and Live. Label each room `room`, or "Main room"
  when `room == ""`. Order as given (first started first).
- **If `rooms.length < 2`:** show nothing new (exactly today's app).
- After choosing room `R`, use that room's own `languages` and `live_english` from its `rooms` entry, and connect
  with `?room=<percent-encoded R>`:
  - `wss://hearchurch.com/c/<slug>/ws/listen?room=R`, with the same hello as today: `{"type":"hello","lang",…}`.
  - `https://hearchurch.com/c/<slug>/stream/<lang>.mp3?room=R`.
  - Send the parameter even when `R == ""` (`?room=`), so the phone stays in the unnamed room.
- On the listener socket, handle `{"type":"rooms","rooms":[names]}` by updating the choice (it may appear or
  disappear mid-meeting). If the phone's room ends (`status` with `live:false`) and `rooms` is not empty, offer the
  remaining rooms, or move to the only one left.
- Live (the room audio over UDP, `_hearchurch-live._udp`): each room's computer runs its own. Pick the Live sender
  on the same computer as the chosen room. The Live TXT record has no room. Match by the Bonjour host: the
  `_hearchurch._tcp` record below, when announced, has the same host as that computer's Live record. Shortcut
  acceptable for now: with 2+ Live senders on the Wi-Fi, let the person choose by service name (the computer's
  name).

### 2. When hearchurch.com can't be reached mid-meeting

Trigger: the listener socket or stream to hearchurch.com fails to reconnect for about 10 s while the phone was
playing a meeting (keep trying the server in the background).

1. Browse Bonjour **`_hearchurch._tcp`** (domain `local.`). Add `_hearchurch._tcp` to the app's `NSBonjourServices`.
   Local Network permission is already asked for Live.
2. Each instance's TXT record:

   | key | value |
   |---|---|
   | `v` | `1` (this contract) |
   | `slug` | the congregation's slug (as in hearchurch.com/<slug>) |
   | `room` | the room name (`""` = unnamed) |
   | `session` | the hub session code the meeting had (may be `""` if it never reached the hub) |

   The service name is the computer's name. The port is the engine's HTTP port (it changes at each launch, so
   always resolve it).
3. Take the instance whose `slug` equals the congregation being played and whose `room` equals the room being
   played (case-insensitive; `""` when the phone used no room). If none matches the room but one matches the slug,
   use it.
4. Resolve its host and port, then play from the computer itself. These are plain HTTP and WS on the Wi-Fi, with no
   key and no cookie:
   - voice: `http://<host>:<port>/stream/<lang>.mp3` (the same never-ending MP3 as the hub's)
   - captions: `ws://<host>:<port>/ws/listen`, hello `{"type":"hello","lang":"<lang>"}` (optionally `boot`,
     `session`, `last_seq` as with the hub). `init` has the same shape as the hub's except it has no
     `congregation`, `next_meeting`, `room` or `rooms`. The messages that follow are the same kinds (`caption`,
     `onair`, `languages`, `lang_state`, `speaker`, `status`).
   - Needs `NSAllowsLocalNetworking` (already set for Live).
5. Go back to hearchurch.com as soon as it answers again (`GET /api/hc/c/<slug>` succeeds and the room is live
   there), or when the Bonjour instance disappears. The computer withdraws it as soon as its own link to the
   server is back.
6. While on the computer, show a small line such as "Listening on this Wi-Fi". Playback should not stop or restart
   more than once at each switch.

A 403 from the computer means it isn't serving the Wi-Fi (the server is reachable from the computer, or the meeting
is over). Go back to the server.
