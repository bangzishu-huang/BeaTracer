"""
analyze.py
----------------
Run this ONCE per song. Extracts a drift-corrected osu!-style timing
grid, onsets snapped to that grid's musical subdivisions, and real
structural drops - saved to a beatmap JSON that main.py loads.

Onset/beat detection runs on the PERCUSSIVE component only (harmonic-
percussive separation) so vocals/lyrics don't get mistaken for rhythm
events. Drop detection uses the full mix, since a real drop legitimately
involves vocals and everything else hitting at once.

Usage:
    python3 analyze.py path/to/song.ogg

Output:
    path/to/song_beatmap.json
"""

import sys
import os
import json
import bisect
import numpy as np

try:
    import librosa
except ImportError:
    print("librosa is not installed. Run: pip install librosa numpy")
    sys.exit(1)


SUBDIVISIONS = (1, 2, 3, 4, 6)
SNAP_TOLERANCE_RATIO = 0.12
TIMING_WINDOW_BEATS = 16


def separate_percussive(y):
    """Splits audio into harmonic (vocals/melody) and percussive (drums/beat) components."""
    _, y_percussive = librosa.effects.hpss(y, margin=3.0)
    return y_percussive


def detect_tempo_and_beats(y, sr):
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0])
    return tempo, beat_times


def detect_onsets(y, sr):
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True, units="frames")
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    return onset_times


def compute_timing_points(beat_times_ms, window_beats=TIMING_WINDOW_BEATS):
    """Fits a LOCAL BPM+Offset every `window_beats` beats - this is what handles tempo drift."""
    n = len(beat_times_ms)
    if n == 0:
        return [{"start_ms": 0.0, "beat_length_ms": 500.0, "offset_ms": 0.0}]
    if n == 1:
        return [{"start_ms": beat_times_ms[0], "beat_length_ms": 500.0, "offset_ms": beat_times_ms[0]}]

    points = []
    i = 0
    prev_beat_length = None
    while i < n:
        chunk_end = min(n, i + window_beats + 1)
        chunk = beat_times_ms[i:chunk_end]

        if len(chunk) < 2:
            beat_length_ms = prev_beat_length if prev_beat_length else 500.0
            offset_ms = chunk[0]
        else:
            idx_arr = np.arange(len(chunk))
            A = np.vstack([idx_arr, np.ones(len(chunk))]).T
            slope, intercept = np.linalg.lstsq(A, np.array(chunk, dtype=float), rcond=None)[0]
            beat_length_ms = float(slope)
            offset_ms = float(intercept)

        points.append({
            "start_ms": float(beat_times_ms[i]),
            "beat_length_ms": beat_length_ms,
            "offset_ms": offset_ms,
        })
        prev_beat_length = beat_length_ms
        i += window_beats

    return points


def _active_timing_point(timing_points, t, starts):
    idx = bisect.bisect_right(starts, t) - 1
    idx = max(0, idx)
    return timing_points[idx]


def snap_onsets_to_grid_piecewise(onset_times_ms, timing_points,
                                   subdivisions=SUBDIVISIONS, tolerance_ratio=SNAP_TOLERANCE_RATIO):
    """Each onset snaps using whichever timing point is active at ITS OWN timestamp."""
    starts = [tp["start_ms"] for tp in timing_points]
    results = []

    for t in onset_times_ms:
        tp = _active_timing_point(timing_points, t, starts)
        beat_length_ms = tp["beat_length_ms"]
        offset_ms = tp["offset_ms"]

        snapped_time = None
        snapped_div = None
        for d in sorted(subdivisions):
            tick_spacing = beat_length_ms / d
            n_ticks = round((t - offset_ms) / tick_spacing)
            tick_time = offset_ms + n_ticks * tick_spacing
            residual = abs(t - tick_time)
            tolerance = tolerance_ratio * tick_spacing
            if residual <= tolerance:
                snapped_time = tick_time
                snapped_div = d
                break

        if snapped_time is None:
            d = max(subdivisions)
            tick_spacing = beat_length_ms / d
            n_ticks = round((t - offset_ms) / tick_spacing)
            snapped_time = offset_ms + n_ticks * tick_spacing
            snapped_div = d

        results.append({"time_ms": int(round(snapped_time)), "subdivision": snapped_div})

    seen = set()
    deduped = []
    for entry in sorted(results, key=lambda e: e["time_ms"]):
        if entry["time_ms"] in seen:
            continue
        seen.add(entry["time_ms"])
        deduped.append(entry)

    return deduped


def detect_drops(y, sr, min_gap_sec=8.0, energy_jump_std=2.0, sustain_sec=1.0):
    """Real structural drops: sustained energy jump vs a long rolling average, not every beat."""
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    long_window_sec = 6.0
    long_window_frames = max(1, int((long_window_sec * sr) / hop_length))
    rolling_avg = np.convolve(rms, np.ones(long_window_frames) / long_window_frames, mode="same")

    jump = rms - rolling_avg
    jump_mean = jump.mean()
    jump_std = jump.std()
    threshold = jump_mean + energy_jump_std * jump_std

    sustain_frames = max(1, int((sustain_sec * sr) / hop_length))
    candidate_idxs = np.where(jump > threshold)[0]

    drops = []
    last_time = -min_gap_sec
    for idx in candidate_idxs:
        t = times[idx]
        if t - last_time < min_gap_sec:
            continue

        end_idx = min(len(rms), idx + sustain_frames)
        if end_idx <= idx:
            continue

        avg_after = rms[idx:end_idx].mean()
        if avg_after > rolling_avg[idx] + (threshold - jump_mean) * 0.5:
            drops.append(float(t))
            last_time = t

    return drops


def analyze(path):
    print(f"Loading {path} ...")
    y, sr = librosa.load(path, sr=None, mono=True)

    print("Separating percussive (beat) content from vocals/melody...")
    y_percussive = separate_percussive(y)

    print("Detecting main beat pulse...")
    tempo, beat_times = detect_tempo_and_beats(y_percussive, sr)
    beat_times_ms = [t * 1000 for t in beat_times]

    print(f"Fitting timing points every {TIMING_WINDOW_BEATS} beats (drift correction)...")
    timing_points = compute_timing_points(beat_times_ms)
    avg_bpm = 60000.0 / np.median([tp["beat_length_ms"] for tp in timing_points])

    print("Detecting onsets (actual note/hit events, percussive only)...")
    onset_times = detect_onsets(y_percussive, sr)
    onset_times_ms = [t * 1000 for t in onset_times]

    print("Snapping onsets to the drift-corrected musical grid...")
    snapped = snap_onsets_to_grid_piecewise(onset_times_ms, timing_points)

    print("Detecting drops (full mix - vocals count toward energy here)...")
    drop_times = detect_drops(y, sr)

    beatmap = {
        "source_file": os.path.basename(path),
        "tempo_bpm": round(float(avg_bpm), 2),
        "timing_points": [
            {"start_ms": round(tp["start_ms"], 1),
             "beat_length_ms": round(tp["beat_length_ms"], 3),
             "offset_ms": round(tp["offset_ms"], 3)}
            for tp in timing_points
        ],
        "beats_ms": [int(t) for t in beat_times_ms],
        "onsets_ms": [e["time_ms"] for e in snapped],
        "onsets_subdivisions": [e["subdivision"] for e in snapped],
        "drops_ms": [int(t * 1000) for t in drop_times],
    }

    out_path = os.path.splitext(path)[0] + "_beatmap.json"
    with open(out_path, "w") as f:
        json.dump(beatmap, f, indent=2)

    div_counts = {d: beatmap["onsets_subdivisions"].count(d) for d in SUBDIVISIONS}
    bpm_range = [tp["beat_length_ms"] for tp in timing_points]
    bpm_min = 60000.0 / max(bpm_range)
    bpm_max = 60000.0 / min(bpm_range)

    print(f"\nDone. Avg tempo ~{beatmap['tempo_bpm']} BPM "
          f"(range across song: {bpm_min:.1f}-{bpm_max:.1f} BPM, {len(timing_points)} timing points)")
    print(f"{len(beatmap['onsets_ms'])} onsets snapped to grid:")
    print(f"  1/1 (whole beat): {div_counts[1]}")
    print(f"  1/2 (half beat):  {div_counts[2]}")
    print(f"  1/3 (triplet):    {div_counts[3]}")
    print(f"  1/4 (stream):     {div_counts[4]}")
    print(f"  1/6 (fast swing): {div_counts[6]}")
    print(f"{len(beatmap['drops_ms'])} drop(s) detected")
    print(f"Saved beatmap to: {out_path}")
    return beatmap


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 analyze.py path/to/song.ogg")
        sys.exit(1)
    analyze(sys.argv[1])