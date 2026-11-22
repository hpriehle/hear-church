# Live: latency budget and how to cut it

Live sends the room audio from the church computer to phones on the Wi-Fi. Every millisecond between the microphone
and the listener's ear is heard as the phone being "behind" the room. Written 2026-11-22 on branch
`chat/hc-live-sync` (from 6b217fb), after Harrison's report: "still slightly off".

## Target

Sound reaches a listener from the loudspeakers at about 2.9 ms per metre (343 m/s). At 5-15 m from the speakers,
the room sound is already 15-45 ms late. A second copy of a sound that arrives 5-30 ms after the first is heard
as one sound (the precedence or Haas effect). For speech that fusion breaks down at about 50 ms
([precedence effect](https://en.wikipedia.org/wiki/Precedence_effect)).

- **Target: 30 ms or less from microphone to ear on the phone. Aim for 20 ms.** A listener then hears the phone and
  the room as one voice from anywhere in the chapel.
- For comparison, commercial Wi-Fi assistive listening systems advertise 31 ms (Listen Auri) and 40-60 ms
  (ListenWIFI). Auracast advertises 30-40 ms
  ([Listen Technologies](https://www.listentech.com/audio-latency-in-assistive-listening/)). Bluetooth headphones
  add 100-300 ms, so Live needs wired headphones.

## The budget, stage by stage

Sources: **B** = `python -m live.latency bench` on this Linux box (real sender, encoder, UDP, listener jitter buffer
and decoder; fake microphone). **C** = the code. **W** = measured on the Wi-Fi here. **R** = research, not measured
here. The phone rows are for iPhone build 4 as described in the sender's code ("phones buffer ~15 ms").

| # | Stage | Before (6b217fb) | Now (this branch) | Phone fixes (§3) | How we know |
|---|---|---|---|---|---|
| 1 | Input device buffer (CoreAudio / WASAPI) | 50 ms stream, default `latency="high"` | Live has its own stream: `latency="low"`, 5 ms blocks | – | C. PortAudio reports the actual figure: logged by the engine and sent in HELLO |
| 2 | 50 ms block, re-clocked into 5 ms frames | **102.6 ms** | **5.3 ms** (own input); 61-64 ms (fallback) | – | B |
| 3 | Opus: 5 ms frame + 2.5 ms look-ahead | 2.5 ms (the frame is counted in row 2) | 2.5 ms | – | C, [RFC 6716 §2](https://datatracker.ietf.org/doc/html/rfc6716) |
| 4 | Encode, packetise, `sendto` | < 0.5 ms | < 0.5 ms | – | B (row 2 minus the 5 ms frame) |
| 5 | Wi-Fi, one way | ~3-15 ms, spikes to 25+ | same, plus DSCP EF (voice queue) | keep the radio awake | W, see below |
| 6 | Phone jitter buffer | ~15 ms fixed | ~15 ms | adaptive, 10-15 ms on good Wi-Fi | C (build 4); B used 15 ms |
| 7 | Phone output IO buffer | ~20-23 ms (iOS default) | ~20-23 ms | **5 ms** | R ([Apple](https://developer.apple.com/library/archive/documentation/Audio/Conceptual/AudioSessionProgrammingGuide/OptimizingForDeviceHardware/OptimizingForDeviceHardware.html)) |
| 8 | DAC and wired headphone | ~1-3 ms | same | (log `outputLatency`) | R, measure on the phone |
| | **Total, mouth to ear** | **~150-160 ms** | **~55-60 ms** | **~30-35 ms** | sum; check with `live.latency wav` |

The bench numbers (median of 17-18 clicks, ideal output device, 15 ms jitter buffer, loopback network):

| Path | capture → packet sent | capture → played by the listener |
|---|---|---|
| Before: meeting's 50 ms blocks (TapSource, 6b217fb) | 102.6 ms | 117.1 ms |
| Before, each block up to 8 ms late | 103.4 ms | 117.4 ms |
| Now, fallback: meeting's blocks with the prebuffer fixed | 61.0-64.3 ms | 77.2-81.4 ms |
| **Now, default: Live's own 5 ms input** | **5.3 ms** | **22.2 ms** |
| Own input, each block up to 8 ms late | 9.6 ms | 25.6 ms |

"Played" includes the 15 ms jitter buffer and the decoder's 2.5 ms. "Played" minus "sent" is about 17 ms on every
path.

### Row 1-2: where 100 ms went, and the fix

The translator reads the microphone in 50 ms blocks. 690c620 found that 10 ms blocks lose 4-9% of the audio while
Python is busy. A second 10 ms stream in the same process dragged the 50 ms one down too. Live took the same
50 ms blocks and re-clocked them into one 5 ms frame every 5 ms, with a 60 ms prebuffer.

**Bug: the prebuffer was really 100 ms.** Blocks arrive 10 frames at a time, so the "12 frames" threshold is first
crossed at 20 frames. From then on the buffer sits between 10 and 20 frames, which is 100 ms behind. A slow
device clock let it creep further up: up to 40 frames (200 ms) before it was trimmed. Fixed in `TapSource`: when
it starts it keeps only the newest 12 frames, and it trims above 15 instead of 40. The bench result went from
102.6 to 61-64 ms.

**The real fix: Live gets its own input, in its own process** (`ChildSender` in `live/sender.py`). For each
meeting the engine starts a child Python process (multiprocessing, spawn). The child opens the same microphone in
5 ms blocks at `latency="low"` (the existing `DeviceSource`), then encodes and sends. Why a separate process works
where the second stream in 690c620 did not:

- **Python lock.** The child has its own GIL. The meeting's Python can't hold up its audio callbacks, and its own
  work is only encode and send: about 18% of one core here, with no listeners.
- **IO buffer size.** In one process, all PortAudio streams on a device share CoreAudio's IO buffer size. That is
  why the 10 ms stream dragged the 50 ms one down: the translator's callback ran on 10 ms deadlines. Separate
  processes can run with different buffer sizes on the same device, and the HAL mixes them
  ([Core Audio list, 2020](https://lists.apple.com/archives/coreaudio-api/2020/May/msg00004.html)). WASAPI shared
  mode gives each client its own stream; the engine period is 10 ms
  ([Microsoft](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/low-latency-audio)).
- **The translator's path is unchanged.** `app/audio_in.py` is byte-identical to 6b217fb: 50 ms blocks, same
  device, same callback. With the own input running, `MicSource.tap` stays `None`, so the translator's callback
  does less work than before.

Fallback: if the child can't open the input, or it stops later (unplugged, no audio for 4 s), Live goes on with
the meeting's 50 ms blocks, now 61-64 ms. That path stays for the rest of the meeting. `/api/status` shows
`live_english.low_latency` true or false, and the engine log says which path runs and why. Choosing another input
during a meeting restarts the child on the new one; phones lose about 1-2 s of audio.

**Must be confirmed on the Mac and Windows dev builds:** the translator still gets 100% of its audio while Live
runs (watch for "input status: input overflow" in the debug log, and translation quality). Check that the log
says "from its own input", and note the capture latency it prints.

The Windows child prefers the WASAPI version of the input. `--exclusive` (WASAPI exclusive mode) would lock the
translator out of the microphone, so it is not used.

Not done: IAudioClient3 shared periods below 10 ms. PortAudio's WASAPI host doesn't use them
([PortAudio #385](https://github.com/PortAudio/portaudio/issues/385)), and they would change the period for
every app on the endpoint.

### Row 3: Opus frame size

Restricted low-delay (CELT only) delay is the frame plus 2.5 ms of MDCT look-ahead. That is 7.5 ms at 5 ms frames
and 5 ms at 2.5 ms frames ([RFC 6716](https://datatracker.ietf.org/doc/html/rfc6716)). PyAV's libopus accepts
`frame_duration=2.5`; checked here: frame size 120. **Kept at 5 ms**, for three reasons:

- it saves only 2.5 ms;
- it doubles the packet rate to 400 per second per phone, which on Wi-Fi costs more airtime and queueing than it
  saves;
- iPhone build 4 is written for 240-sample frames.

If it is ever wanted, HELLO already carries `frame_samples`. A phone that reads it could take 120; build 4
probably can't, so it would need a protocol version bump. Redundancy (each packet also carries the previous frame)
is kept.

### Row 4: send pacing, backlog and thread priority

- Each frame goes out as soon as it is encoded. If the sender ever falls behind (a stall), it now skips to the
  newest frame once more than 4 frames (20 ms) are queued. Before, a stall's backlog stayed as extra delay on the
  phone until its drift correction slowly removed it, at one frame per second.
- On the own-input path, PortAudio's callback runs on the system's real-time audio thread (CoreAudio's IO thread;
  on Windows, PortAudio's WASAPI thread). The encode thread is the only other busy thread in the child. The bench
  measured under 0.5 ms from a frame being complete to it being sent, so raising its priority wouldn't buy anything
  measurable.

### Row 5: Wi-Fi

Measured from this box (wired) to a Wi-Fi client on the home network, with pings of 64 bytes:

| Pings every | RTT median | p95 | p99 | max |
|---|---|---|---|---|
| 5 ms (Live's packet rate) | 7.1 ms | 29 ms | 47 ms | 57 ms |
| 1 s | avg 16.7 ms | – | – | 89 ms |
| 0.2 s | avg 12.6 ms | – | – | 139 ms |

Half the round trip is roughly the one-way delay, so about 3-15 ms with spikes past 25 ms. A device with sparse
traffic shows the classic power-save pattern: tens of ms while the radio wakes, or waits for the access point's
beacon. Steady traffic helps, but it doesn't remove the spikes.

What the sender does and what to do at the church:

- **DSCP EF (TOS 0xB8)** is set on Live's socket (IPv4 `IP_TOS`, IPv6 `IPV6_TCLASS`). On macOS it also sets
  `SO_NET_SERVICE_TYPE = NET_SERVICE_TYPE_VO`. Access points that follow RFC 8325 send EF from the WMM voice queue
  (AC_VO), which goes ahead of all other traffic ([RFC 8325 §4.3](https://datatracker.ietf.org/doc/html/rfc8325)).
  It is best effort: Windows ignores `IP_TOS` unless a QoS policy is set (below). A test checks that the byte
  arrives as 0xB8.
- **Windows:** to make the marking stick, add a Group Policy QoS policy: Computer Configuration → Windows Settings →
  Policy-based QoS. Set application `hearchurch-engine.exe`, DSCP 46, UDP, source port 47300. Without it,
  marking needs qWAVE, which needs admin rights.
- **Connect the church computer by Ethernet.** On Wi-Fi, every packet crosses the air twice (computer → access
  point → phone), which doubles row 5's wobble.
- Unicast is kept on purpose. Multicast on Wi-Fi goes out at the slowest rate and waits for the DTIM beacon
  (100-300 ms) whenever any phone sleeps.

### Rows 6-8: the phone

See §3 below.

## 3. Phone-side changes (hc-mobile, `modules/live-english`, Swift)

These are for the Mac session that has the iPhone repo. The order is by gain. No sender change is needed; the
wire protocol is unchanged (version 1).

1. **Ask for a 5 ms output buffer at 48 kHz, and check what you got.** This saves about 15-18 ms. iOS uses about
   20 ms by default, and the request is only a hint, so read the values back after activation
   ([Apple, Optimizing for device hardware](https://developer.apple.com/library/archive/documentation/Audio/Conceptual/AudioSessionProgrammingGuide/OptimizingForDeviceHardware/OptimizingForDeviceHardware.html),
   [QA1631](https://developer.apple.com/library/archive/qa/qa1631/_index.html)):
   ```swift
   let s = AVAudioSession.sharedInstance()
   try s.setCategory(.playback, mode: .default, options: [])   // no .mixWithOthers: it lowers our priority for small buffers
   try s.setPreferredSampleRate(48_000)                        // the stream is 48 kHz: no resampler in the path
   try s.setPreferredIOBufferDuration(0.005)                   // 240 frames
   try s.setActive(true)
   log("io \(s.ioBufferDuration * 1000) ms, out \(s.outputLatency * 1000) ms, sr \(s.sampleRate), route \(s.currentRoute.outputs.map(\.portType))")
   ```
   Apply it again after `routeChangeNotification` and `interruptionNotification` end. Plugging in headphones can
   reset the buffer.
   - Mode: keep `.default`, or try `.measurement`, which asks iOS for the least signal processing. Never use
     `.voiceChat`: it requires `.playAndRecord` and turns on voice processing (echo cancellation, AGC). That adds
     delay and changes the sound. `.playback` needs no microphone permission.

2. **Pull from the jitter buffer inside the render callback.** If build 4 queues decoded buffers with
   `AVAudioPlayerNode.scheduleBuffer`, every queued buffer is extra delay. Use `AVAudioSourceNode` (or a RemoteIO
   render callback) that pops and decodes the next frame on demand:
   ```swift
   let node = AVAudioSourceNode(format: AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!) { _, _, frames, abl in
       jitter.render(into: UnsafeMutableAudioBufferListPointer(abl)[0], frames: Int(frames))  // 240-sample frames, carry the remainder
       return noErr
   }
   engine.attach(node); engine.connect(node, to: engine.mainMixerNode, format: nil)
   ```
   Decode in the render path. One 5 ms Opus decode costs tens of µs. When a frame is missing, call
   `opus_decode(dec, nil, 0, pcm, 240, 0)` (Opus's own concealment), or use the redundant copy from the next packet.

3. **Make the jitter buffer adaptive.** A fixed 15 ms is too much on a quiet network and too little in a spike.
   This follows WebRTC NetEq's approach (an arrival-time histogram that forgets slowly; target between a floor and
   most of the buffer;
   [delay_manager.cc](https://webrtc.googlesource.com/src/+/175aa2e95c19a1e02786b09602c6f62b3554f1a2/modules/audio_coding/neteq/delay_manager.cc)):
   - For each AUDIO packet: `transit = arrival - seq * 0.005`. Keep the last 10 s, and only the minimum and a
     sorted window are needed. `spread = p97(transit) - min(transit)`.
   - `target = clamp(ceil(spread / 5 ms) + 1, 2, 12)` frames, which is 10-60 ms.
   - Raise it at once when a frame is missing at play time; that is an underrun.
   - Lower it slowly: drop one frame only when depth > target + 1 for 200 ms, preferably during a quiet frame
     (|peak| < -40 dBFS), so the cut is inaudible.
   - Start a stream at 3 frames, not at the old fixed value.
   - Show `target × 5 ms` in a debug line next to `ioBufferDuration` and `outputLatency`, so the total is visible.

4. **Keep the Wi-Fi radio awake and mark the phone's own packets as voice.** 200 packets per second already arrive,
   but iOS power save still adds wake-up delay (Apple lists "the receiving Wi-Fi driver entering low-power mode"
   as a cause of delay: [forum 45210](https://developer.apple.com/forums/thread/45210)).
   - Send PING every 100 ms instead of every 1 s. The sender answers each one. The steady two-way traffic keeps the
     radio active, and the RTT is free telemetry. Keep SUBSCRIBE at about 1 s.
   - Mark the socket as voice. With Network.framework: `let p = NWParameters.udp; p.serviceClass = .interactiveVoice`.
     With a BSD socket: `var v: Int32 = 4 /* NET_SERVICE_TYPE_VO */; setsockopt(fd, SOL_SOCKET, 0x1116 /*
     SO_NET_SERVICE_TYPE */, &v, socklen_t(4))`. That puts the phone's uplink in the voice queue, and iOS treats
     the flow as real-time.
   - Measure before and after with `live.latency wav` (below). The gain is not proven here.

5. **Output latency: show it, and warn about Bluetooth.** Audio can't be played earlier than it arrives, so there
   is nothing to compensate. Log `outputLatency` (wired is usually a few ms; USB-C and Lightning adapters add a
   little). If `currentRoute.outputs` contains `.bluetoothA2DP` or `.bluetoothLE`, show a line under the player:
   "Bluetooth headphones are about 0.2 s behind the room; wired ones keep up."

6. **Don't add delay the sender already reports.** HELLO's last field is the sender's capture latency in 0.1 ms
   units. It is about 5-10 ms on the own-input path and 60 ms on the fallback (it was 0 before this branch). A debug
   overlay can then show the full chain: `capture + 7.5 + rtt/2 + jitter target + io + outputLatency`.

## 4. Measuring it

`live/latency.py` has two commands:

- `uv run --extra linux python -m live.latency bench [--path device|tap] [--late-ms 8] [--buffer-ms 15]`
  runs on any computer, needs no audio hardware, and repeats to within about 1 ms. It runs the real sender and the
  real listener. It covers rows 2, 3, 4 and 6 (Wi-Fi replaced by loopback). Use it to compare sender builds. A test
  (`test_tap_path_delay_is_its_prebuffer_not_a_block_more`) keeps the fallback path at its prebuffer.
- **`python -m live.latency wav recording.m4a` measures the real mouth-to-ear delay.** This is the number to compare
  phone builds with. You need the church computer running a meeting with Live on, the Live phone with its wired
  headphones, and a second recorder (any phone's voice memo app).
  1. Turn the room speakers off, or turn the PA down: the clap must reach the recorder only directly and through
     the headphone.
  2. Put the recorder right next to the church computer's microphone. Hold one earbud against the recorder's mic.
  3. Record and clap sharply 5-10 times, 1 s or more apart, near the microphone.
  4. Run `python -m live.latency wav recording.m4a`. It prints each gap and the median. The gap covers everything:
     microphone, computer, Wi-Fi, phone, headphone.

  The analysis finds each clap, then the sharpest rise in loudness 8-400 ms after it. A test checks it on a
  synthetic clap with room tail to within about 1 ms.
- `python -m live.listen <ip> --no-play` (existing) on a laptop in the chapel measures that spot's Wi-Fi: loss,
  jitter p95/p99, and RTT. It prints a suggested jitter buffer. Its "mic-to-ear" estimate now includes the
  fallback path's 60 ms, because HELLO reports it.

This box has no audio devices, and its Wi-Fi is an access point with no clients. So the full acoustic test and a
Wi-Fi listener test have to be run at the church or on the Mac. The Wi-Fi numbers above come from a wired ping to
a Wi-Fi client.

## Still to measure on real hardware

1. The Mac dev build: the log line, plus `live.latency wav` before and after (expect about 150 → about 55 ms with
   phone build 4).
2. The translator's input is still complete while the child runs: no input overflows in the debug log, and a
   normal transcript, on the MacBook and on Windows.
3. After the phone changes in §3: `live.latency wav` again (expect about 30-35 ms).
