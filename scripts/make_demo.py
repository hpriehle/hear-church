"""Make demo/ (the Hear Church Demo's recorded meeting, app/hear/demo.py) from one run of the real pipeline.

Run once, offline, then commit demo/:
  uv run --extra linux python -m app --file samples/sermon.mp3 --start 22 --duration 180 --record --exit-when-done
  uv run python scripts/make_demo.py logs/session-<stamp>.jsonl
The run records each language's stream (logs/session-<stamp>-<lang>.mp3) and every line's caption and on-air
times (the .jsonl). This keeps the stretch from the first caption to the last voice, long silences shortened, as small
mono MP3s, plus demo/timeline.json: the captions and on-air marks in seconds from its start.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "demo"
KBPS, SR = 32, 22050
MAX_GAP_S = 1.5   # a silence in every language longer than this is shortened to it (the run's slow CPU made them long)


def loud(mp3: Path) -> np.ndarray:
    """Per 10 ms of the recording: is there sound."""
    pcm = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", str(mp3), "-ac", "1", "-ar", "8000", "-f", "s16le",
                                        "-"], capture_output=True, check=True).stdout, dtype=np.int16)
    return (np.abs(pcm[:len(pcm) // 80 * 80]).reshape(-1, 80).max(axis=1) > 300)


def kept(sound: np.ndarray, begin: float, end: float) -> list[tuple[float, float]]:
    """The stretches of [begin, end] to keep: all but the middle of every silence longer than MAX_GAP_S."""
    out, a, i, n = [], begin, int(begin * 100), int(end * 100)
    while i < n:
        if sound[i]:
            i += 1
            continue
        j = i
        while j < n and not sound[j]:
            j += 1
        if (j - i) / 100 > MAX_GAP_S:
            out.append((a, i / 100 + MAX_GAP_S / 2))
            a = j / 100 - MAX_GAP_S / 2
        i = j
    return out + [(a, end)]


def main(jsonl: Path) -> None:
    records = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    langs = list(records[0]["langs"])
    mp3 = {c: jsonl.with_name(f"{jsonl.stem}-{c}.mp3") for c in langs}
    # where the recording starts on the run's clock: the first voice on air is the first sound in it
    sound = {c: loud(mp3[c]) for c in langs}
    zero = float(np.mean([min(r["langs"][c]["t_on_air"] for r in records if r["langs"][c]["t_on_air"])
                          - int(np.argmax(sound[c])) / 100 for c in langs]))
    events = []
    for c in langs:
        lines = sorted((r["langs"][c] for r in records if r["langs"][c]["t_caption"]), key=lambda x: x["t_caption"])
        for n, x in enumerate(lines, 1):
            events.append({"t": x["t_caption"] - zero, "lang": c, "n": n, "text": x["text"]})
            if x["t_on_air"]:
                events.append({"t": x["t_on_air"] - zero, "lang": c, "n": n, "end": x["t_on_air"] + x["clip_s"] - zero})
    begin = min(e["t"] for e in events) - 1.0
    end = max(e.get("end", e["t"]) for e in events) + 2.0
    n = min(len(x) for x in sound.values())
    keep = kept(np.any([x[:n] for x in sound.values()], axis=0), begin, min(end, n / 100))
    period = sum(b - a for a, b in keep)
    for e in events:   # a caption that fell in a cut silence shows where the cut is
        e["t"] = round(sum(max(0.0, min(b, e["t"]) - a) for a, b in keep), 2)
        e.pop("end", None)
    events.sort(key=lambda e: e["t"])
    OUT.mkdir(exist_ok=True)
    for c in langs:
        select = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in keep)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3[c]), "-af", f"aselect='{select}',asetpts=N/SR/TB",
                        "-ac", "1", "-ar", str(SR), "-b:a", f"{KBPS}k", "-write_xing", "0", "-id3v2_version", "0",
                        "-map_metadata", "-1", str(OUT / f"{c}.mp3")], check=True)
    (OUT / "timeline.json").write_text(json.dumps(
        {"source": "\"The Happy Little Girl\" (J. C. Ryle, The Two Bears and Other Sermons for Children), LibriVox, "
                   "public domain; translated and voiced by the Hear Church pipeline", "languages": langs,
         "period_s": round(period, 2), "bitrate_kbps": KBPS, "events": events}, ensure_ascii=False, indent=0) + "\n")
    print(f"{OUT}: {', '.join(langs)}, {period:.0f} s, {len(events)} events")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
