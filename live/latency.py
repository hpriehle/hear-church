"""Measure Live's delay, so builds can be compared with numbers instead of by ear.

    python -m live.latency bench                  # capture -> phone playout, on this computer (no audio hardware)
    python -m live.latency bench --path tap       # the engine's 50 ms blocks re-clocked to 5 ms frames
    python -m live.latency bench --late-ms 8      # each capture block up to 8 ms late (a busy Python)
    python -m live.latency wav clap.wav           # mouth-to-ear from a recording (see below)

bench: a fake input puts a click into the audio every 0.5 s at a known instant. It runs the real sender (encoder,
UDP) and the real test listener (jitter buffer, Opus decoder) over loopback, and times each click when its packet
leaves and when the listener plays it. Not included: the input device's own buffer, Wi-Fi, and the phone's
output buffer and headphones (see docs/live-latency.md for those).

wav, the real mouth-to-ear: hold the Live phone's headphone against a second recorder (any phone's voice memo
app), put both right next to the church computer's microphone, turn the room speakers off, and clap a few
times, 1 s or more apart. Each clap is in the recording twice: directly, then from the headphone. The gap is
the whole chain: mic, computer, Wi-Fi, phone, headphone.
"""
from __future__ import annotations

import argparse
import random
import threading
import time

import numpy as np

from . import protocol as P

SR = P.SAMPLE_RATE
CLICK_EVERY = SR // 2          # samples between clicks
CLICK = np.full(48, 20000, np.int16)  # 1 ms


class FakeCapture:
    """Calls `deliver(int16 block)` every `block` samples on the clock, like a device callback, with an optional
    random lateness per block. A click starts every 0.5 s; `captured(n)` is when click n was 'at the microphone'."""

    def __init__(self, deliver, block: int, late_ms: float = 0.0):
        self.deliver, self.block, self.late = deliver, block, late_ms / 1000
        self.t0 = 0.0
        self._stop = threading.Event()

    def captured(self, n: int) -> float:
        return self.t0 + (n + 1) * CLICK_EVERY / SR

    def run(self) -> None:
        self.t0 = time.perf_counter()
        k = 0
        while not self._stop.is_set():
            idx = np.arange(k, k + self.block)
            x = np.zeros(self.block, np.int16)
            pos = idx % CLICK_EVERY
            hit = (pos < len(CLICK)) & (idx >= CLICK_EVERY)
            x[hit] = CLICK[pos[hit]]
            k += self.block
            due = self.t0 + k / SR + random.uniform(0, self.late)
            time.sleep(max(0.0, due - time.perf_counter()))
            self.deliver(x)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def onset(pcm: np.ndarray) -> int | None:
    i = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 4000)
    return int(i[0]) if len(i) else None


def bench(path: str = "device", seconds: float = 10.0, buffer_ms: float = 15.0, late_ms: float = 0.0) -> dict:
    from .listen import Listener
    from .sender import LiveSender, Source, TapSource

    if path == "tap":
        src = TapSource()
        cap = FakeCapture(lambda x: src.push(x.astype(np.float32) / 32767, SR), SR // 20, late_ms)  # 50 ms blocks
    else:
        src = Source()
        cap = FakeCapture(src._put, P.FRAME_SAMPLES, late_ms)  # 5 ms blocks, like the low-latency input
    sender = LiveSender(src, port=0, bind="127.0.0.1")
    sent: list[float] = []
    send_frame = sender.send_frame

    def timed_send(pcm):
        if onset(pcm) is not None:
            sent.append(time.perf_counter())
        send_frame(pcm)
    sender.send_frame = timed_send
    L = Listener("127.0.0.1", sender.port, buffer_ms)
    played: list[float] = []
    stop = threading.Event()

    def playout():  # an ideal output device: frame starts playing the moment it is pulled
        nxt = time.perf_counter()
        armed = True
        while not stop.is_set():
            now = time.perf_counter()
            pcm = L.next_pcm()
            i = onset(pcm)
            if i is not None and armed:
                played.append(now + i / SR)
            armed = i is None
            nxt += P.FRAME_SAMPLES / SR
            time.sleep(max(0.0, nxt - time.perf_counter()))
    sender.start()
    L.start()
    threading.Thread(target=playout, daemon=True).start()
    cap.start()
    time.sleep(seconds)
    cap.stop()
    stop.set()
    L.stop()
    sender.stop()
    ms = lambda ts: np.array([(t - cap.captured(n)) * 1000 for n, t in enumerate(ts, 1)])
    s, p = ms(sent[1:]), ms(played[1:])  # the first click goes out while buffers are still filling
    return {"path": path, "clicks": len(p), "send_ms": (float(np.median(s)), float(np.max(s))) if len(s) else None,
            "play_ms": (float(np.median(p)), float(np.max(p))) if len(p) else None,
            "lost": L.counts["lost"], "recovered": L.counts["recovered"]}


def clap_gaps(x: np.ndarray, sr: int, min_ms: float = 8, max_ms: float = 400) -> list[float]:
    """Gaps (ms) between each clap and its copy from the headphone. A clap is a jump to >= 30% of the loudest
    sound; its copy is the sharpest rise in loudness 8-400 ms after it."""
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    w = max(1, sr // 1000)
    env = np.convolve(np.abs(x), np.ones(w) / w, mode="same")
    thr = 0.3 * env.max()
    out, i = [], 0
    lo, hi, back = int(sr * min_ms / 1000), int(sr * max_ms / 1000), int(sr * 0.005)
    while True:
        above = np.flatnonzero(env[i:] > thr)
        if not len(above):
            return out
        c = i + int(above[0])
        seg = env[c + lo - back: c + hi]
        if len(seg) <= back:
            return out
        # rise = loudness now vs the loudest of the 1-5 ms before it (the clap's own echo dies away smoothly)
        prior = np.array([seg[j - back:j - w].max() for j in range(back, len(seg))])
        rise = seg[back:] / (prior + 1e-9)
        out.append((lo + int(np.argmax(rise))) * 1000 / sr)
        i = c + int(sr * 0.8)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m live.latency", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench", help="capture -> playout on this computer")
    b.add_argument("--path", choices=["device", "tap"], default="device",
                   help="device: Live's own 5 ms input; tap: the meeting's 50 ms blocks")
    b.add_argument("--seconds", type=float, default=10)
    b.add_argument("--buffer-ms", type=float, default=15, help="listener jitter buffer (the phone's is ~15 ms)")
    b.add_argument("--late-ms", type=float, default=0, help="each capture block up to this late")
    w = sub.add_parser("wav", help="mouth-to-ear from a recording of claps and the phone's headphone")
    w.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "bench":
        r = bench(a.path, a.seconds, a.buffer_ms, a.late_ms)
        fmt = lambda v: f"median {v[0]:5.1f} ms, worst {v[1]:5.1f} ms" if v else "no clicks"
        print(f"{r['path']}: {r['clicks']} clicks, jitter buffer {a.buffer_ms:.0f} ms, blocks up to {a.late_ms:.0f} ms late")
        print(f"  capture -> packet sent  {fmt(r['send_ms'])}")
        print(f"  capture -> played       {fmt(r['play_ms'])}   (lost {r['lost']}, recovered {r['recovered']})")
        return
    import av
    with av.open(a.file) as f:
        st = f.streams.audio[0]
        sr = st.rate
        x = np.concatenate([fr.to_ndarray().mean(axis=0) for fr in f.decode(st)])
    gaps = clap_gaps(x, sr)
    if not gaps:
        raise SystemExit("No claps found: clap louder, nearer the recorder.")
    print("gaps: " + ", ".join(f"{g:.0f}" for g in gaps) + " ms")
    print(f"mouth-to-ear: median {np.median(gaps):.0f} ms over {len(gaps)} claps")


if __name__ == "__main__":
    main()
