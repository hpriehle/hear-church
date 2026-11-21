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


def test_tap_source_turns_device_blocks_into_48k_frames():
    from live.sender import TapSource
    src = TapSource()
    t = np.arange(441 * 100) / 44100
    x = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
    for i in range(100):  # 1 s of 10 ms blocks from a 44.1 kHz input
        src.push(x[i * 441:(i + 1) * 441], 44100)
    frames = [src.frames.get_nowait()[0] for _ in range(src.frames.qsize())]
    assert 190 <= len(frames) <= 200 and all(f.dtype == np.int16 and len(f) == 240 for f in frames)
    assert np.abs(np.concatenate(frames[20:])).max() > 10000


def test_meeting_sends_its_input_while_live_english_is_on(monkeypatch):
    from types import SimpleNamespace

    import live.sender as S
    from app.audio_in import MicSource
    from app.pipeline import Pipeline

    monkeypatch.setattr(S, "LiveSender", lambda src, _real=S.LiveSender: _real(src, port=0, bind="127.0.0.1"))
    monkeypatch.setattr(S, "advertise", lambda *a: None)   # no Bonjour from a test
    mic = MicSource.__new__(MicSource)
    mic.tap = None
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
        mic.tap(np.full(480, 0.3, np.float32), 48000)   # what the microphone callback hands on
        assert P.unpack(c.recvfrom(2048)[0]).type == P.PacketType.AUDIO
        assert p.live_english() == {"on": True, "sending": True, "listeners": 1}
        c.close()
    finally:
        p.settings.audio.live_english = False   # toggle off: stops, and the input is no longer tapped
        p._sync_live()
    assert p.live is None and mic.tap is None
