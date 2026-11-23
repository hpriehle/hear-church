"""Live English sender: capture the room audio on the office computer and stream it to phones.

    python -m live.sender --list-devices
    python -m live.sender --device "Line In"          # the receiver's aux out
    python -m live.sender --tone                       # test tone, no audio hardware needed
    python -m live.sender --file sermon.mp3            # play a recording as if it were live

Audio: 48 kHz mono, Opus 5 ms frames in low-delay mode (64 kbps), each packet also carrying the
previous frame. Sent by UDP to every listener that has subscribed in the last 5 seconds.
"""
from __future__ import annotations

import argparse
import logging
import math
import queue
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import protocol as P
from .opus import Encoder

log = logging.getLogger("live.sender")

MAX_LISTENERS = 250


# ---------------------------------------------------------------- audio sources
class Source:
    """Produces 240-sample int16 frames into `self.frames` in real time."""

    latency_ms = 0.0

    def __init__(self):
        self.frames: queue.Queue[tuple[np.ndarray, float]] = queue.Queue(maxsize=200)
        self.dropped = 0  # frames the encoder fell behind on
        self.overflows = 0  # driver-reported input overflows

    def _put(self, pcm: np.ndarray) -> None:
        try:
            self.frames.put_nowait((pcm, time.perf_counter()))
        except queue.Full:
            self.dropped += 1

    def start(self) -> None: ...
    def stop(self) -> None: ...


class PacedSource(Source):
    """Base for synthetic sources: emits a frame every 5 ms from a thread."""

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def next_frame(self) -> np.ndarray | None:
        raise NotImplementedError

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, name="paced-source", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(1)

    def _run(self) -> None:
        period = P.FRAME_SAMPLES / P.SAMPLE_RATE
        nxt = time.perf_counter()
        while not self._stop.is_set():
            pcm = self.next_frame()
            if pcm is None:
                return
            self._put(pcm)
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:  # stalled (laptop asleep, debugger): don't burst to catch up
                nxt = time.perf_counter()


class ToneSource(PacedSource):
    """A 440 Hz tone with a short beep pattern, so dropouts are easy to hear."""

    def __init__(self, freq: float = 440.0, level_db: float = -18.0):
        super().__init__()
        self._n = 0
        self._amp = 32767 * 10 ** (level_db / 20)
        self._freq = freq

    def next_frame(self) -> np.ndarray:
        idx = np.arange(self._n, self._n + P.FRAME_SAMPLES)
        self._n += P.FRAME_SAMPLES
        on = (idx % P.SAMPLE_RATE) < P.SAMPLE_RATE * 0.6  # 0.6 s on, 0.4 s off
        x = np.sin(2 * math.pi * self._freq * idx / P.SAMPLE_RATE) * self._amp * on
        return x.astype(np.int16)


class FileSource(PacedSource):
    """Any audio/video file decoded by PyAV, played in real time, looping."""

    def __init__(self, path: str, loop: bool = True):
        super().__init__()
        self._path, self._loop = path, loop
        self._buf = np.zeros(0, np.int16)
        self._it = self._decode()

    def _decode(self):
        import av
        while True:
            with av.open(self._path) as f:
                rs = av.AudioResampler(format="s16", layout="mono", rate=P.SAMPLE_RATE)
                for frame in f.decode(audio=0):
                    for r in rs.resample(frame):
                        yield r.to_ndarray().reshape(-1)
            if not self._loop:
                return

    def next_frame(self) -> np.ndarray | None:
        while len(self._buf) < P.FRAME_SAMPLES:
            chunk = next(self._it, None)
            if chunk is None:
                return None
            self._buf = np.concatenate([self._buf, chunk])
        out, self._buf = self._buf[:P.FRAME_SAMPLES], self._buf[P.FRAME_SAMPLES:]
        return out


class DeviceSource(Source):
    """A live input device via PortAudio. On Windows this uses WASAPI."""

    def __init__(self, device: int | str | None, channel: str | int = "mix", exclusive: bool = False):
        super().__init__()
        import sounddevice as sd
        self._sd = sd
        self.device = resolve_device(device)
        info = sd.query_devices(self.device)
        self.device_name = info["name"]
        hostapi = sd.query_hostapis(info["hostapi"])["name"]
        self.hostapi = hostapi
        ch = {"mix": 0, "left": 1, "right": 2}.get(channel, channel)   # or an input channel number (1 = first)
        self._channels = min(int(info["max_input_channels"]), ch) if ch else min(2, int(info["max_input_channels"]))
        self._col = ch - 1 if ch and ch <= self._channels else None
        extra = None
        if "WASAPI" in hostapi:
            try:
                extra = sd.WasapiSettings(exclusive=exclusive, auto_convert=not exclusive)
            except TypeError:  # older sounddevice without auto_convert
                extra = sd.WasapiSettings(exclusive=exclusive)
        self._buf = np.zeros(0, np.int16)
        self._stream = sd.InputStream(
            device=self.device, samplerate=P.SAMPLE_RATE, channels=self._channels,
            dtype="int16", blocksize=P.FRAME_SAMPLES, latency="low",
            extra_settings=extra, callback=self._callback)
        self.latency_ms = float(self._stream.latency) * 1000

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread: keep it short
        if status.input_overflow:
            self.overflows += 1
        x = indata
        if x.shape[1] == 1:
            mono = x[:, 0]
        elif self._col is not None:
            mono = x[:, self._col]
        else:
            mono = ((x[:, 0].astype(np.int32) + x[:, 1]) // 2).astype(np.int16)
        if frames == P.FRAME_SAMPLES and not len(self._buf):
            self._put(mono.copy())
            return
        self._buf = np.concatenate([self._buf, mono])
        while len(self._buf) >= P.FRAME_SAMPLES:
            self._put(self._buf[:P.FRAME_SAMPLES].copy())
            self._buf = self._buf[P.FRAME_SAMPLES:]

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()


class TapSource(PacedSource):
    """Audio pushed in by the meeting engine (its own capture of the room input, 50 ms blocks at the device's rate),
    sent on as one 5 ms frame every 5 ms: phones buffer ~15 ms, so 50 ms bursts would break up. The engine's capture
    keeps its 50 ms blocks because 10 ms ones lose audio while Python is busy (4-9% on a MacBook, 2026-11-21)."""

    PREBUFFER = 12   # frames (60 ms): one 50 ms block plus slack, so the steady 5 ms clock never runs dry
    MAX = 15         # frames: more queued (a stall, or the device's clock running fast) and we skip to the newest
    latency_ms = PREBUFFER * P.FRAME_MS   # told to phones in HELLO: the delay this path adds before sending

    def __init__(self):
        super().__init__()
        self._in: deque[tuple[np.ndarray, int]] = deque(maxlen=100)
        self._rs = None
        self._sr = 0
        self._buf = np.zeros(0, np.int16)
        self._primed = False

    def push(self, mono: np.ndarray, sr: int) -> None:
        """float32 mono samples in -1..1; called from the capture callback, so it only queues them."""
        self._in.append((mono, sr))

    def next_frame(self) -> np.ndarray:
        import soxr
        while self._in:
            mono, sr = self._in.popleft()
            if sr != self._sr:  # first block, or the device reopened at another rate
                self._rs, self._sr = soxr.ResampleStream(sr, P.SAMPLE_RATE, 1, dtype="float32", quality="LQ"), sr
            x = self._rs.resample_chunk(mono) if sr != P.SAMPLE_RATE else mono
            self._buf = np.concatenate([self._buf, (np.clip(x, -1, 1) * 32767).astype(np.int16)])
        n = P.FRAME_SAMPLES
        if len(self._buf) > self.MAX * n:
            self._buf = self._buf[-self.PREBUFFER * n:]
        if len(self._buf) >= (n if self._primed else self.PREBUFFER * n):
            if not self._primed:  # blocks come 10 frames at a time: start at the target, not up to a block above it
                self._buf = self._buf[-self.PREBUFFER * n:]
            self._primed = True
            out, self._buf = self._buf[:n], self._buf[n:]
            return out
        self._primed = False   # ran dry: silence until a full prebuffer is back
        return np.zeros(n, np.int16)


def list_devices() -> list[tuple[int, str, str, int]]:
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], sd.query_hostapis(d["hostapi"])["name"], d["max_input_channels"]))
    return out


def resolve_device(device: int | str | None) -> int | None:
    """Index, or the first input whose name contains `device`, preferring WASAPI on Windows."""
    if device is None or isinstance(device, int):
        return device
    if device.isdigit():
        return int(device)
    matches = [d for d in list_devices() if device.lower() in d[1].lower()]
    if not matches:
        raise SystemExit(f"No input device matching {device!r}. Run with --list-devices.")
    matches.sort(key=lambda d: ("WASAPI" not in d[2], d[1].lower() != device.lower()))
    return matches[0][0]


# ---------------------------------------------------------------- sender
@dataclass
class Stats:
    frames: int = 0
    packets_sent: int = 0
    send_errors: int = 0
    peak: int = 0
    sum_sq: float = 0.0
    n: int = 0
    started: float = field(default_factory=time.monotonic)

    def level_db(self) -> tuple[float, float]:
        rms = math.sqrt(self.sum_sq / self.n) if self.n else 0
        db = lambda v: 20 * math.log10(v / 32768) if v > 0 else -99.0
        return db(rms), db(self.peak)


class LiveSender:
    def __init__(self, source: Source, port: int = P.DEFAULT_PORT, bind: str = "0.0.0.0",
                 bitrate: int = 64000, redundancy: bool = True, channel: int = 0,
                 clock=time.monotonic):
        self.source = source
        self.channel = channel
        self.redundancy = redundancy
        self.encoder = Encoder(bitrate)
        self.clock = clock
        if bind == "0.0.0.0" and socket.has_dualstack_ipv6():
            # IPv6 too: a Mac's Bonjour also gives phones its IPv6 addresses, and iOS tries those first
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            bind = "::"
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        # DSCP EF: Wi-Fi access points send it from their voice queue (WMM AC_VO, RFC 8325). Best effort: Windows
        # ignores it without a QoS policy, and each option applies only to some socket families / systems.
        # 0x1116 = SO_NET_SERVICE_TYPE, 4 = NET_SERVICE_TYPE_VO: macOS's own way to ask for the voice class
        for opt in ((socket.IPPROTO_IP, socket.IP_TOS, 0xB8), (socket.IPPROTO_IPV6, getattr(socket, "IPV6_TCLASS", -1), 0xB8),
                    *([(socket.SOL_SOCKET, 0x1116, 4)] if sys.platform == "darwin" else [])):
            try:
                self.sock.setsockopt(*opt)
            except OSError:
                pass
        self.sock.bind((bind, port))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.5)
        self.subscribers: dict[tuple[str, int], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats = Stats()
        self._seq = 0
        self._prev: bytes | None = None
        self._ts = 0

    # -- control traffic
    def handle(self, data: bytes, addr: tuple[str, int]) -> None:
        p = P.unpack(data)
        if p is None:
            return
        now = self.clock()
        if p.type == P.PacketType.SUBSCRIBE:
            with self._lock:
                if addr not in self.subscribers and len(self.subscribers) >= MAX_LISTENERS:
                    return
                if addr not in self.subscribers:
                    log.info("listener joined %s:%d", addr[0], addr[1])
                self.subscribers[addr] = now
                n = len(self.subscribers)
            self._send(P.Packet(P.PacketType.HELLO, self.channel,
                                payload=P.hello_payload(n, self.source.latency_ms)), addr)
        elif p.type == P.PacketType.UNSUBSCRIBE:
            with self._lock:
                if self.subscribers.pop(addr, None) is not None:
                    log.info("listener left %s:%d", addr[0], addr[1])
        elif p.type == P.PacketType.PING:
            self._send(P.Packet(P.PacketType.PONG, self.channel, payload=p.payload[:64]), addr)

    def prune(self) -> None:
        cutoff = self.clock() - P.SUBSCRIBER_TIMEOUT_S
        with self._lock:
            for addr in [a for a, t in self.subscribers.items() if t < cutoff]:
                del self.subscribers[addr]
                log.info("listener timed out %s:%d", addr[0], addr[1])

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                self.prune()
                continue
            except ConnectionResetError:  # Windows: ICMP port unreachable from a gone listener
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            self.handle(data, addr)

    # -- audio
    def _send(self, p: P.Packet, addr) -> bool:
        try:
            self.sock.sendto(P.pack(p), addr)
            return True
        except OSError:
            self.stats.send_errors += 1
            return False

    def send_frame(self, pcm: np.ndarray) -> None:
        s = self.stats
        a = np.abs(pcm.astype(np.int32))
        s.peak = max(s.peak, int(a.max()))
        s.sum_sq += float(np.dot(a, a))
        s.n += len(a)
        for opus_frame in self.encoder.encode(pcm):
            flags, payload = P.audio_payload(opus_frame, self._prev if self.redundancy else None)
            pkt = P.pack(P.Packet(P.PacketType.AUDIO, self.channel, flags, self._seq, self._ts, payload))
            with self._lock:
                targets = list(self.subscribers)
            for addr in targets:
                try:
                    self.sock.sendto(pkt, addr)
                    s.packets_sent += 1
                except OSError:
                    s.send_errors += 1
            self._prev = opus_frame
            self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._ts = (self._ts + P.FRAME_SAMPLES) & 0xFFFFFFFF
        s.frames += 1

    def _audio_loop(self) -> None:
        while not self._stop.is_set():
            try:
                pcm, _ = self.source.frames.get(timeout=0.5)
                while self.source.frames.qsize() > 4:  # fell behind (a stall): skip to the newest, or the delay stays
                    pcm, _ = self.source.frames.get_nowait()
                    self.source.dropped += 1
            except queue.Empty:
                continue
            self.send_frame(pcm)

    # -- lifecycle
    def start(self) -> None:
        for fn, name in ((self._recv_loop, "recv"), (self._audio_loop, "audio")):
            t = threading.Thread(target=fn, name=f"live-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.source.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()
        for t in self._threads:
            t.join(1)
        self.sock.close()

    def listener_count(self) -> int:
        with self._lock:
            return len(self.subscribers)


# ---------------------------------------------------------------- discovery
def local_ip() -> str:
    """The address phones should use: the one on the interface with the default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_addresses(ip: str) -> list[str]:
    """IPv4 addresses phones on the Wi-Fi can reach: the default route's first, never loopback, link-local or
    Tailscale (100.64.0.0/10)."""
    import ipaddress

    import ifaddr  # comes with zeroconf
    found = [ip] + [a.ip for ad in ifaddr.get_adapters() for a in ad.ips if isinstance(a.ip, str)]
    out = []
    for a in found:
        x = ipaddress.ip_address(a)
        if not (x.is_loopback or x.is_link_local or x in ipaddress.ip_network("100.64.0.0/10")) and a not in out:
            out.append(a)
    return out


def advertise(name: str, ip: str, port: int, kind: str = P.MDNS_TYPE, props: dict[str, str] | None = None):
    """Advertise over mDNS/Bonjour (Live's service unless `kind`/`props` say otherwise). Returns a closer or None."""
    props = {"v": str(P.VERSION), "ch": "en"} if props is None else props
    if sys.platform == "darwin":
        return _advertise_mac(name, port, kind, props)
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf not installed: phones need the IP address (%s:%d)", ip, port)
        return None
    addrs = lan_addresses(ip)
    if not addrs:
        log.warning("mDNS: no network address phones could reach (only %s)", ip)
        return None
    # One address (the default route's): phones may try only the first, and bridges (Docker, Hyper-V) can't be reached
    info = ServiceInfo(kind, f"{name}.{kind}", addresses=[socket.inet_aton(addrs[0])], port=port, properties=props)
    zc = Zeroconf(interfaces=addrs)
    try:
        zc.register_service(info)
    except Exception as e:
        log.warning("mDNS advertise failed: %s", e)
        zc.close()
        return None

    def close():
        zc.unregister_service(info)
        zc.close()
    return close


def _advertise_mac(name: str, port: int, kind: str, props: dict[str, str]):
    """Through the Mac's own Bonjour (mDNSResponder): it announces on every network with the right address. The app
    may not send multicast itself (Local Network: DenyMulticast), so zeroconf only reached lo0 (2026-11-21).
    The registration ends when this process does."""
    import ctypes
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    ref = ctypes.c_void_p()
    txt = b"".join(bytes([len(kv)]) + kv for kv in (f"{k}={v}".encode()[:255] for k, v in props.items()))
    err = lib.DNSServiceRegister(ctypes.byref(ref), 0, 0, name.encode(), kind.removesuffix(".local.").encode(),
                                 None, None, ctypes.c_uint16(socket.htons(port)), len(txt), txt, None, None)
    if err:
        log.warning("Bonjour advertise failed: error %d", err)
        return None
    return lambda: lib.DNSServiceRefDeallocate(ref)


# ---------------------------------------------------------------- own input, in its own process
class ChildSender:
    """Live in a child process with its own 5 ms input stream on the meeting's microphone: ~55 ms sooner than the
    tap. Its own process, so the meeting's Python never delays its callbacks, and its small input buffer can't drag
    down the translator's 50 ms one (as a second stream in the same process did, 690c620): CoreAudio and WASAPI
    give each process its own buffer. Raises if it can't open the input; `on_exit(self)` if it stops by itself."""

    low_latency = True

    def __init__(self, device: str | None, channel: int, name: str, on_exit=None, timeout: float = 20.0):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")   # a fresh interpreter: no forked meeting threads or PortAudio state
        self._stop, self._listeners = ctx.Event(), ctx.Value("i", 0)
        rx, tx = ctx.Pipe(duplex=False)
        self._proc = ctx.Process(target=_child_main, args=(device, channel, name, self._stop, self._listeners, tx),
                                 name="live-sender", daemon=True)
        self._proc.start()
        tx.close()
        try:
            msg = rx.recv() if rx.poll(timeout) else "did not start in time"
        except EOFError:
            msg = f"exited with code {self._proc.exitcode}"
        if isinstance(msg, str):
            self.stop()
            raise RuntimeError(f"Live's own input: {msg}")
        self.port, self.latency_ms = msg
        threading.Thread(target=self._watch, args=(on_exit,), name="live-child", daemon=True).start()

    def _watch(self, on_exit) -> None:
        self._proc.join()
        if not self._stop.is_set() and on_exit:
            on_exit(self)

    def listener_count(self) -> int:
        return self._listeners.value

    def stop(self) -> None:
        self._stop.set()
        self._proc.join(3)
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(1)


def _child_main(device, channel, name, stop, listeners, conn) -> None:
    import multiprocessing as mp
    try:
        source = DeviceSource(device, channel)
        sender = LiveSender(source)
        close = advertise(name, local_ip(), sender.port)
        sender.start()
    except (Exception, SystemExit) as e:
        conn.send(f"{type(e).__name__}: {e}")
        return
    conn.send((sender.port, source.latency_ms))
    parent, last, quiet = mp.parent_process(), -1, 0.0
    try:
        while not stop.wait(0.5):
            listeners.value = sender.listener_count()
            quiet = quiet + 0.5 if sender.stats.frames == last else 0.0
            last = sender.stats.frames
            # input gone (unplugged) or the engine gone: stop, and the engine falls back to its own input
            if quiet >= 4 or (parent is not None and not parent.is_alive()):
                return
    finally:
        sender.stop()
        if close:
            close()


# ---------------------------------------------------------------- CLI
def meter(db: float, width: int = 20) -> str:
    filled = max(0, min(width, int((db + 60) / 60 * width)))
    return "#" * filled + "-" * (width - filled)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m live.sender", description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--device", help="input device index or part of its name (default: system default)")
    src.add_argument("--tone", action="store_true", help="send a beeping test tone")
    src.add_argument("--file", help="send an audio file in real time (loops)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--channel", choices=["mix", "left", "right"], default="mix",
                    help="which input channel to use on a stereo input")
    ap.add_argument("--exclusive", action="store_true", help="WASAPI exclusive mode (lowest delay)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--bitrate", type=int, default=64000)
    ap.add_argument("--no-redundancy", action="store_true")
    ap.add_argument("--name", default=socket.gethostname(), help="name shown to phones (mDNS)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if a.list_devices:
        for i, name, api, ch in list_devices():
            print(f"{i:3d}  {name}  [{api}, {ch} ch]")
        return

    if a.tone:
        source: Source = ToneSource()
        label = "test tone"
    elif a.file:
        source = FileSource(a.file)
        label = a.file
    else:
        try:
            source = DeviceSource(a.device, a.channel, a.exclusive)
        except Exception as e:
            raise SystemExit(f"Could not open the audio input: {e}\n"
                             "Try --list-devices, a different --device, or drop --exclusive.")
        label = f"{source.device_name} [{source.hostapi}{', exclusive' if a.exclusive else ''}]"

    sender = LiveSender(source, a.port, bitrate=a.bitrate, redundancy=not a.no_redundancy)
    ip = local_ip()
    close_mdns = advertise(a.name, ip, sender.port)
    sender.start()
    print(f"Live English sender: {label}")
    print(f"Listeners connect to {ip}:{sender.port}  (capture latency {source.latency_ms:.1f} ms)")
    print("Ctrl+C to stop.\n")

    def _terminate(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGBREAK"):  # service stop / console close on Windows
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _terminate)

    try:
        last_frames = 0
        while True:
            time.sleep(1)
            s = sender.stats
            rms, peak = sender.stats.level_db()
            fps = s.frames - last_frames
            last_frames = s.frames
            warn = ""
            if peak > -1:
                warn = "  CLIPPING: turn the input down"
            elif rms < -55 and s.n:
                warn = "  (silent)"
            sys.stdout.write(
                f"\rlevel {meter(rms)} {rms:6.1f} dB  peak {peak:6.1f}  | listeners {sender.listener_count():3d}"
                f"  | {fps:3d} frames/s  drops {source.dropped + source.overflows}{warn}      ")
            sys.stdout.flush()
            s.peak, s.sum_sq, s.n = 0, 0.0, 0
    except KeyboardInterrupt:
        print()
    finally:
        sender.stop()
        if close_mdns:
            close_mdns()


if __name__ == "__main__":
    main()
