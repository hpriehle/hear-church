"""Live English sender/listener: protocol, jitter buffer and a localhost round trip."""
import socket
import time

import numpy as np
import pytest

pytest.importorskip("av")

from live import protocol as P  # noqa: E402
from live.listen import JitterBuffer, Listener  # noqa: E402
from live.opus import Decoder, Encoder  # noqa: E402
from live.sender import LiveSender, ToneSource  # noqa: E402


def test_packet_round_trip():
    flags, payload = P.audio_payload(b"abc", b"xy")
    raw = P.pack(P.Packet(P.PacketType.AUDIO, 0, flags, 0xFFFFFFFF, 123, payload))
    p = P.unpack(raw)
    assert p.type == P.PacketType.AUDIO and p.seq == 0xFFFFFFFF and p.ts == 123
    assert P.split_audio(p) == (b"abc", b"xy")
    assert P.split_audio(P.Packet(P.PacketType.AUDIO, payload=b"abc")) == (b"abc", None)
    assert P.unpack(b"XX" + raw[2:]) is None
    assert P.unpack(raw[:5]) is None
    assert P.unpack(raw[:3] + bytes([99]) + raw[4:]) is None


def test_hello_and_seq_wrap():
    h = P.parse_hello(P.hello_payload(3, 10.25))
    assert (h.sample_rate, h.frame_samples, h.listeners) == (48000, 240, 3)
    assert h.capture_latency_ms == pytest.approx(10.2)
    assert P.seq_diff(1, 0xFFFFFFFF) == 2
    assert P.seq_diff(0xFFFFFFFF, 1) == -2


def test_opus_frames_are_5ms_and_decode():
    enc, dec = Encoder(), Decoder()
    t = np.arange(240 * 20) / 48000
    x = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    out = []
    for i in range(20):
        pkts = enc.encode(x[i * 240:(i + 1) * 240])
        assert len(pkts) == 1 and len(pkts[0]) == 40  # 64 kbps CBR, one packet per frame
        out.append(dec.decode(pkts[0]))
    assert all(len(o) == 240 for o in out)
    assert np.abs(np.concatenate(out[5:])).max() > 4000


def test_jitter_buffer_reorders_waits_and_recovers():
    jb = JitterBuffer(target_frames=3)
    assert jb.pull() == (None, "wait")
    jb.push(10, b"a", None)
    jb.push(12, b"c", b"b")  # 11 missing, but 12 carries a copy of it
    assert jb.pull() == (b"a", "ok")  # depth 3 reached
    assert jb.pull() == (b"b", "recovered")
    jb.push(11, b"b-late", None)  # arrived after it was played
    assert jb.late == 1
    assert jb.pull() == (b"c", "ok")
    assert jb.pull() == (None, "lost")


def test_jitter_buffer_handles_seq_wrap_and_refills_after_outage():
    jb = JitterBuffer(target_frames=2)
    jb.push(0xFFFFFFFF, b"x", None)
    jb.push(0, b"y", b"x")
    assert [jb.pull()[0] for _ in range(2)] == [b"x", b"y"]
    for _ in range(20):
        jb.pull()
    assert not jb.playing and jb.next is None  # refills from the next packet
    jb.push(500, b"z", None)
    assert jb.pull() == (None, "wait")


def test_jitter_buffer_jumps_ahead_after_stall():
    jb = JitterBuffer(target_frames=2, max_frames=10)
    for s in range(30):
        jb.push(s, bytes([s]), None)
    assert jb.depth() <= 10
    assert jb.pull()[0][0] >= 20  # skipped the stale backlog


def _subscribe(sock, port):
    sock.sendto(P.pack(P.Packet(P.PacketType.SUBSCRIBE)), ("127.0.0.1", port))


def test_sender_streams_to_subscriber_and_drops_it():
    clock = [1000.0]
    sender = LiveSender(ToneSource(), port=0, bind="127.0.0.1", clock=lambda: clock[0])
    sender.start()
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(2)
    try:
        _subscribe(c, sender.port)
        types, audio = [], []
        deadline = time.time() + 3
        while len(audio) < 30 and time.time() < deadline:
            p = P.unpack(c.recvfrom(2048)[0])
            types.append(p.type)
            if p.type == P.PacketType.AUDIO:
                audio.append(p)
        assert types[0] == P.PacketType.HELLO
        seqs = [p.seq for p in audio]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        # each packet carries the previous one's frame
        assert P.split_audio(audio[5])[1] == P.split_audio(audio[4])[0]

        c.sendto(P.pack(P.Packet(P.PacketType.PING, payload=b"12345678")), ("127.0.0.1", sender.port))
        assert sender.listener_count() == 1
        clock[0] += P.SUBSCRIBER_TIMEOUT_S + 1
        sender.prune()
        assert sender.listener_count() == 0
    finally:
        c.close()
        sender.stop()


def test_listener_plays_tone_end_to_end():
    sender = LiveSender(ToneSource(), port=0, bind="127.0.0.1")
    sender.start()
    L = Listener("127.0.0.1", sender.port, buffer_ms=20)
    L.start()
    try:
        deadline = time.time() + 3
        while L.jb.depth() < 4 and time.time() < deadline:
            time.sleep(0.01)
        pcm = np.concatenate([L.next_pcm() for _ in range(40)])
        assert np.abs(pcm).max() > 2000
        assert L.counts["ok"] > 0 and L.hello is not None
        time.sleep(1.2)
        assert L.rtts and L.rtts[-1] < 100
    finally:
        L.stop()
        sender.stop()


def test_tap_source_turns_device_blocks_into_paced_48k_frames():
    from live.sender import TapSource
    src = TapSource()
    t = np.arange(2205 * 20) / 44100
    x = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
    first = src.next_frame()
    assert not first.any()                                    # nothing yet: silence, never a gap
    for i in range(20):  # 1 s of 50 ms blocks from a 44.1 kHz input, all at once (the paced thread drains them)
        src.push(x[i * 2205:(i + 1) * 2205], 44100)
    f = src.next_frame()
    assert f.dtype == np.int16 and len(f) == 240 and np.abs(f).max() > 10000   # one 5 ms frame per call
    assert len(src._buf) == (src.PREBUFFER - 1) * 240         # a burst beyond MAX skips to the newest audio
    while src._primed:
        src.next_frame()
    assert not src.next_frame().any()                         # ran dry: silence until refilled


def test_meeting_sends_its_input_while_live_english_is_on(monkeypatch):
    from types import SimpleNamespace

    import live.sender as S
    from app.audio_in import MicSource
    from app.pipeline import Pipeline

    monkeypatch.setattr(S, "LiveSender", lambda src, _real=S.LiveSender: _real(src, port=0, bind="127.0.0.1"))
    monkeypatch.setattr(S, "advertise", lambda *a: None)   # no Bonjour from a test

    def no_own_input(*a, **k):
        raise RuntimeError("no such input")
    monkeypatch.setattr(S, "ChildSender", no_own_input)    # Live's own input can't open: the meeting's is tapped
    mic = MicSource.__new__(MicSource)
    mic.tap, mic.name, mic.wanted, mic._switch_to = None, "Mic", ("", 0), None
    p = Pipeline.__new__(Pipeline)
    p.settings = SimpleNamespace(audio=SimpleNamespace(live_english=True), engine=SimpleNamespace(name="Test"))
    p.worker, p.capturing = SimpleNamespace(source=mic), True
    p._sync_live()
    try:
        assert p.live is not None and mic.tap is not None
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(2)
        _subscribe(c, p.live[0].port)
        assert P.unpack(c.recvfrom(2048)[0]).type == P.PacketType.HELLO
        mic.tap(np.full(2400, 0.3, np.float32), 48000)  # what the microphone callback hands on (50 ms)
        assert P.unpack(c.recvfrom(2048)[0]).type == P.PacketType.AUDIO
        assert p.live_english() == {"on": True, "sending": True, "listeners": 1, "low_latency": False}
        c.close()
    finally:
        p.settings.audio.live_english = False   # toggle off: stops, and the input is no longer tapped
        p._sync_live()
    assert p.live is None and mic.tap is None


def test_announced_addresses_are_the_lan_not_loopback_or_tailscale(monkeypatch):
    from types import SimpleNamespace

    import ifaddr

    import live.sender as S

    ips = ["127.0.0.1", "100.94.10.36", "169.254.21.172", "192.168.1.207", "10.0.0.5", ("fe80::1", 0, 0)]
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [SimpleNamespace(ips=[SimpleNamespace(ip=i) for i in ips])])
    assert S.lan_addresses("192.168.1.207") == ["192.168.1.207", "10.0.0.5"]
    assert S.lan_addresses("127.0.0.1") == ["192.168.1.207", "10.0.0.5"]   # no default route: still the LAN


def test_sender_answers_phones_on_ipv6_and_ipv4():
    if not socket.has_dualstack_ipv6():
        pytest.skip("no dual-stack sockets here")
    sender = LiveSender(ToneSource(), port=0)   # the default: every address, as in a meeting
    sender.start()
    try:
        for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
            c = socket.socket(family, socket.SOCK_DGRAM)
            c.settimeout(2)
            c.sendto(P.pack(P.Packet(P.PacketType.SUBSCRIBE)), (host, sender.port))
            assert P.unpack(c.recvfrom(2048)[0]).type == P.PacketType.HELLO
            c.close()
        assert sender.listener_count() == 2
    finally:
        sender.stop()


def test_live_prefers_its_own_input_follows_a_switch_and_falls_back(monkeypatch):
    from types import SimpleNamespace

    import live.sender as S
    from app.audio_in import MicSource
    from app.pipeline import Pipeline

    started = []

    class FakeChild:
        low_latency, port, latency_ms = True, 1, 5.0

        def __init__(self, device, channel, name, on_exit=None):
            started.append((device, channel))
            self.on_exit, self.stopped = on_exit, False

        def listener_count(self):
            return 3

        def stop(self):
            self.stopped = True
    monkeypatch.setattr(S, "ChildSender", FakeChild)
    monkeypatch.setattr(S, "LiveSender", lambda src, _real=S.LiveSender: _real(src, port=0, bind="127.0.0.1"))
    monkeypatch.setattr(S, "advertise", lambda *a: None)
    mic = MicSource.__new__(MicSource)
    mic.tap, mic.name, mic.wanted, mic._switch_to = None, "MacBook Pro Microphone", ("", 0), None
    p = Pipeline.__new__(Pipeline)
    p.settings = SimpleNamespace(audio=SimpleNamespace(live_english=True), engine=SimpleNamespace(name="Test"))
    p.worker, p.capturing = SimpleNamespace(source=mic), True
    p._sync_live()
    first = p.live[0]
    assert started == [("MacBook Pro Microphone", 0)] and mic.tap is None   # the pinned input, not tapped
    assert p.live_english() == {"on": True, "sending": True, "listeners": 3, "low_latency": True}
    mic._switch_to = ("Line In", 2)   # chosen during the meeting, not switched yet
    p._sync_live()
    assert first.stopped and started[-1] == ("Line In", 2)
    p._sync_live()
    assert len(started) == 2          # same input: left running
    p.live[0].on_exit(p.live[0])      # its input stopped (unplugged): Live goes on, on the meeting's input
    try:
        assert p.live is not None and not p.live_english()["low_latency"] and mic.tap is not None
        p._sync_live()
        assert len(started) == 2      # stays on the fallback for this meeting
    finally:
        p.settings.audio.live_english = False
        p._sync_live()
    assert p.live is None and mic.tap is None


def test_own_input_process_reports_a_missing_input():
    from live.sender import ChildSender
    with pytest.raises(RuntimeError, match="own input"):
        ChildSender("no input has this name", 0, "Test")


def test_child_sends_its_input_until_told_to_stop(monkeypatch):
    import threading
    from multiprocessing import Event, Pipe, Value

    import live.sender as S
    monkeypatch.setattr(S, "DeviceSource", lambda device, channel: ToneSource())
    monkeypatch.setattr(S, "LiveSender", lambda src, _real=S.LiveSender: _real(src, port=0, bind="127.0.0.1"))
    monkeypatch.setattr(S, "advertise", lambda *a: None)
    stop, n = Event(), Value("i", 0)
    rx, tx = Pipe(duplex=False)
    t = threading.Thread(target=S._child_main, args=("Mic", 0, "Test", stop, n, tx))
    t.start()
    port, _ = rx.recv()
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(2)
    try:
        _subscribe(c, port)
        assert {P.unpack(c.recvfrom(2048)[0]).type for _ in range(5)} >= {P.PacketType.HELLO, P.PacketType.AUDIO}
        time.sleep(0.7)
        assert n.value == 1
    finally:
        c.close()
        stop.set()
        t.join(3)
    assert not t.is_alive()


def test_audio_is_marked_as_voice_for_wifi():
    if not hasattr(socket, "IP_RECVTOS") or not hasattr(socket.socket, "recvmsg"):   # Windows: no recvmsg
        pytest.skip("can't read the TOS byte here")
    sender = LiveSender(ToneSource(), port=0)
    sender.start()
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
    c.settimeout(2)
    try:
        c.sendto(P.pack(P.Packet(P.PacketType.SUBSCRIBE)), ("127.0.0.1", sender.port))
        _, anc, _, _ = c.recvmsg(2048, 64)
        assert [d[0] for lvl, typ, d in anc if typ == socket.IP_TOS] == [0xB8]   # DSCP EF -> WMM voice
    finally:
        c.close()
        sender.stop()


def test_tap_path_delay_is_its_prebuffer_not_a_block_more():
    from live.latency import bench
    r = bench("tap", seconds=2.5)
    assert r["clicks"] >= 3 and 55 < r["send_ms"][0] < 66   # was 102.6 ms at 6b217fb


def test_clap_gap_found_in_a_recording():
    from live.latency import clap_gaps
    sr, rng = 48000, np.random.default_rng(1)
    x = rng.normal(0, 20, sr * 3)
    for c in (sr // 2, sr * 2):   # a clap with a 150 ms room tail, then the headphone's copy 87 ms later, quieter
        tail = rng.normal(0, 1, sr * 15 // 100) * np.exp(-np.arange(sr * 15 // 100) / (sr * 0.03))
        x[c:c + len(tail)] += 20000 * tail
        d = c + sr * 87 // 1000
        x[d:d + len(tail)] += 6000 * tail
    gaps = clap_gaps(x, sr)
    assert len(gaps) == 2 and all(abs(g - 87) <= 1.5 for g in gaps)   # to within a millisecond or so
