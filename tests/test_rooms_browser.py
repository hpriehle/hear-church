"""The web listener's room picker in a real browser (Playwright, Chromium) against a hub on a local port: one room live
= no picker; two rooms = the picker above the languages, the first room chosen, and a tap moves the captions and the
sound to the other room. Then the Hear Church Demo (HC_DEMO=1) plays its recorded captions.
Not a project dependency: run with `uv run --with playwright pytest tests/test_rooms_browser.py`.
HC_SCREENS=1 writes the screenshots to docs/screens/rooms/."""
import asyncio
import json
import os
import socket
import threading
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect

from app import main
from app.hear import demo
from test_hear import hear, make_cong  # noqa: F401  (fixture)

pw = pytest.importorskip("playwright.sync_api")

IPHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True,
          "locale": "en-US"}


@pytest.fixture
def hub(hear):  # noqa: F811
    """(port, loop): the hub on its own event loop in a thread, so the test can start the demo on that loop."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_until_complete, args=(srv.serve(),), daemon=True)
    t.start()
    while not srv.started:
        threading.Event().wait(0.02)
    yield port, loop
    srv.should_exit = True
    t.join(5)


def engine(port, token, languages, room=None):
    """A Mac app that starts a meeting (in a room, or the unnamed one)."""
    st = main.S.settings.stream
    ws = connect(f"ws://127.0.0.1:{port}/ws/engine", additional_headers={"Authorization": f"Bearer {token}"})
    ws.send(json.dumps({"type": "hello", "name": "Mac", "languages": list(languages),
                        "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
    ws.send(json.dumps({"type": "session_start", "languages": list(languages), **({"room": room} if room else {})}))
    while (m := json.loads(ws.recv(3)))["type"] != "session_started":
        assert m["type"] != "session_refused", m
    return ws


def say(ws, lang, seq, text):
    ws.send(json.dumps({"type": "hub", "lang": lang, "remember": True,
                        "msg": {"type": "caption", "seq": seq, "lang": lang, "text": text, "ts": seq}}))


def test_room_picker_and_demo(hear, hub, tmp_path, monkeypatch):  # noqa: F811
    port, loop = hub
    base = f"http://127.0.0.1:{port}"
    out = Path("docs/screens/rooms") if os.environ.get("HC_SCREENS") else tmp_path
    out.mkdir(parents=True, exist_ok=True)
    cong, token = make_cong(hear)

    with pw.sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = b.new_context(**IPHONE).new_page()
        captions = pg.locator("#captions")
        picker = pg.locator("#rooms button")

        def shot(name):
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(out / f"{name}.png"))

        def sound():
            """The stream the page plays (it starts a second after the page knows the meeting is live)."""
            pg.wait_for_function("window.__listener.audio.src.includes('/stream/')")
            return pg.evaluate("window.__listener.audio.src")

        # ---- one room live: no picker
        main_mac = engine(port, token, ["fr", "es"])
        say(main_mac, "fr", 1, "Bonjour de la salle principale")
        pg.goto(base + "/dakar-ward#french")
        captions.get_by_text("Bonjour de la salle principale").wait_for()
        assert pg.locator("#rooms").is_hidden() and pg.locator("#langs").is_visible()
        assert "/c/dakar-ward/stream/fr.mp3" in sound()
        shot("01-one-room")

        # ---- a second room goes live: the picker appears above the languages, the first room chosen
        rs_mac = engine(port, hear.db.add_device(cong["id"], "Second Mac"), ["fr"], room="Relief Society")
        say(rs_mac, "fr", 1, "Bienvenue à la Société de Secours")
        pg.locator("#rooms").wait_for(state="visible")
        assert picker.all_inner_texts() == ["Main room", "Relief Society"]
        assert [x.get_attribute("aria-pressed") for x in picker.all()] == ["true", "false"]
        assert pg.locator("#rooms").bounding_box()["y"] < pg.locator("#langs").bounding_box()["y"]
        assert "Bienvenue" not in captions.inner_text()
        shot("02-two-rooms")

        # ---- tap the other room: its captions, its sound
        picker.nth(1).click()
        captions.get_by_text("Bienvenue à la Société de Secours").wait_for()
        assert "Bonjour" not in captions.inner_text()
        pg.wait_for_function("document.querySelector('#rooms button[aria-pressed=true]')?.textContent === 'Relief Society'")
        pg.wait_for_function("window.__listener.audio.src.includes('room=Relief%20Society')")
        say(rs_mac, "fr", 2, "Ouvrons nos Écritures")
        say(main_mac, "fr", 2, "Pas pour cette salle")
        captions.get_by_text("Ouvrons nos Écritures").wait_for()
        assert "Pas pour cette salle" not in captions.inner_text()
        shot("03-other-room")

        # ---- and back to the main room
        picker.nth(0).click()
        captions.get_by_text("Pas pour cette salle").wait_for()
        assert "Ouvrons" not in captions.inner_text()
        pg.wait_for_function("window.__listener.audio.src.endsWith('&room=')")   # "" = the unnamed room
        shot("04-back-to-main")
        main_mac.close()
        rs_mac.close()

        # ---- the Hear Church Demo: always live, plays its recorded captions
        monkeypatch.setenv("HC_DEMO", "1")
        asyncio.run_coroutine_threadsafe(_start_demo(hear), loop).result(5)   # as the hub's start-up does
        pg.goto(base + "/hear-church-demo#spanish")
        captions.locator("*").first.wait_for(timeout=10000)
        assert pg.locator("#rooms").is_hidden()
        assert pg.inner_text("header .title") == "Hear Church Demo"
        assert "/c/hear-church-demo/stream/es.mp3" in sound()
        pg.wait_for_function("window.__listener.audio.currentTime > 0", timeout=30000)   # the recording plays
        assert pg.inner_text("#audioState") == "Live"
        shot("05-demo")
        b.close()
    for s in list(hear.live.sessions.values()):
        if getattr(s, "replay", None):
            loop.call_soon_threadsafe(s.replay.cancel)


async def _start_demo(hear):  # noqa: F811
    assert demo.enabled()
    return demo.start(hear.db, hear.live)
