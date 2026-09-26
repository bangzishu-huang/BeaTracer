"""
main.py - BeaTracer
-------------------------------------------------------------------
Tap / hold-slider / spinner rhythm gameplay, driven by real audio
analysis (see analyze.py). Visual design follows a dark-minimal,
single-accent system (sky blue, with a secondary violet reserved for
drops/spinners as a "rare/special" indicator) - deliberately avoiding
a per-note rainbow palette or decorative panels.

Workflow:
    1. python3 analyze.py song.ogg      -> creates song_beatmap.json
    2. python3 main.py                  -> loads song.ogg + its beatmap

Fonts: tries Lexend first, then Inter, then a system sans-serif.
Download either from fonts.google.com/specimen/Lexend (or /Inter) and
place the .ttf files next to this script (or in a "fonts/" subfolder)
to use the real thing - otherwise a clean system font is used instead.

Controls: move mouse to hit taps and trace hold-lines, spin your
cursor around spinners. ESC to quit.
"""

import pygame
import sys
import os
import json
import math

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
SCREEN_W, SCREEN_H = 900, 700
FPS = 60

AUDIO_PATH = "1.ogg"
FALLBACK_BPM = 120

BEATS_AHEAD_VISIBLE = 2.0
HIT_WINDOW_MS = 150
HOLD_GRACE_MS = 120
JUDGE_300_FRAC = 0.4
JUDGE_100_FRAC = 0.75

TARGET_RADIUS = 40
DROP_RADIUS_MULT = 1.6
DROP_SCORE_MULT = 2
VANISH_SHRINK = 6
HIT_TOLERANCE_PAD = 15
FOCAL_LENGTH = 300
APPROACH_RING_EXTRA = 0.9

FAST_RUN_MIN_LENGTH = 3
LOCAL_MEDIAN_WINDOW = 5
FAST_GAP_RATIO = 0.65
FAST_GAP_FLOOR_MS = 150
DEFAULT_LOCAL_GAP_MS = 600
FAST_SUBDIVISIONS = (3, 4, 6)

SPINNER_DURATION_MS = 1800
SPINNER_MIN_RADIUS = 30
SPINNER_REQUIRED_ROTATIONS = 2.5

COMBO_MILESTONES = (10, 25, 50)  # plus every 100 beyond that
COMBO_PULSE_MS = 130

# ---------------------------------------------------------------
# PALETTE - one primary accent, used consistently; secondary accent
# reserved for drops/spinners only. No gradients, no per-note rainbow.
# ---------------------------------------------------------------
BG = (13, 15, 18)              # #0D0F12
SURFACE = (21, 24, 29)         # #15181D
ELEVATED = (28, 32, 38)        # #1C2026 - note fill (darker center)
BORDER = (42, 48, 56)          # #2A3038

TEXT_PRIMARY = (244, 247, 250)   # #F4F7FA
TEXT_SECONDARY = (168, 176, 186)  # #A8B0BA
TEXT_MUTED = (105, 113, 124)      # #69717C

ACCENT = (125, 211, 252)         # #7DD3FC - primary, used for normal notes/UI
ACCENT_SECONDARY = (167, 139, 250)  # #A78BFA - drops/spinners only

SUCCESS = (110, 231, 183)   # #6EE7B7 - "100" judgement
WARNING = (251, 191, 36)    # #FBBF24 - "50" judgement
ERROR = (251, 113, 133)     # #FB7185 - miss


# ---------------------------------------------------------------
# FONT LOADING - Lexend first, then Inter, then system fallback
# ---------------------------------------------------------------
_FONT_CACHE = {}
_font_source_reported = False


def load_font(size, bold=False):
    global _font_source_reported
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    candidates = (
        ["Lexend-Bold.ttf", "fonts/Lexend-Bold.ttf", "Inter-Bold.ttf", "fonts/Inter-Bold.ttf"] if bold
        else ["Lexend-Regular.ttf", "fonts/Lexend-Regular.ttf", "Lexend.ttf", "fonts/Lexend.ttf",
              "Inter-Regular.ttf", "fonts/Inter-Regular.ttf", "Inter.ttf", "fonts/Inter.ttf"]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                font = pygame.font.Font(path, size)
                if not _font_source_reported:
                    print(f"Using local font file: {path}")
                _FONT_CACHE[key] = font
                return font
            except Exception:
                continue

    for name in ["Lexend", "Inter", "Helvetica Neue", "Helvetica", "Arial", "Segoe UI", "SF Pro Display"]:
        try:
            font = pygame.font.SysFont(name.replace(" ", ""), size, bold=bold)
            if font:
                if not _font_source_reported:
                    print(f"Lexend/Inter .ttf not found locally - using system font '{name}' instead. "
                          f"For the real thing, download Lexend-Regular.ttf / Lexend-Bold.ttf "
                          f"from fonts.google.com/specimen/Lexend and place them next to main.py.")
                    _font_source_reported = True
                _FONT_CACHE[key] = font
                return font
        except Exception:
            continue

    font = pygame.font.SysFont(None, size, bold=bold)
    _FONT_CACHE[key] = font
    return font


def draw_text_with_shadow(screen, font, text, color, pos, center=False):
    shadow = font.render(text, True, (4, 5, 6))
    main_surf = font.render(text, True, color)
    if center:
        rect = main_surf.get_rect(center=pos)
        screen.blit(shadow, (rect.x + 1, rect.y + 1))
        screen.blit(main_surf, rect)
    else:
        screen.blit(shadow, (pos[0] + 1, pos[1] + 1))
        screen.blit(main_surf, pos)


def luminance(color):
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


# ---------------------------------------------------------------
# NOTE VISUAL HELPERS
# ---------------------------------------------------------------
def draw_tap_circle(screen, cx, cy, radius, accent_color):
    """Dark center, accent-colored outline, subtle highlight - per spec."""
    radius = max(2, int(radius))
    pygame.draw.circle(screen, ELEVATED, (int(cx), int(cy)), radius)
    pygame.draw.circle(screen, accent_color, (int(cx), int(cy)), radius, width=3)
    hl_radius = max(1, int(radius * 0.22))
    hl_pos = (int(cx - radius * 0.3), int(cy - radius * 0.3))
    hl_color = tuple(min(255, int(c * 1.25)) for c in accent_color)
    pygame.draw.circle(screen, hl_color, hl_pos, hl_radius)


def draw_approach_ring(screen, sx, sy, radius, progress, color):
    """Never thicker than the note itself - thin outline that contracts onto it."""
    extra = radius * APPROACH_RING_EXTRA * progress
    approach_radius = radius + extra
    if approach_radius > 1:
        pygame.draw.circle(screen, color, (int(sx), int(sy)), int(approach_radius), width=2)


def draw_pill_node(screen, fill_color, cx, cy, diameter):
    """Solid filled circle - builds the slider's continuous pill/tube body."""
    radius = max(2, int(diameter / 2))
    pygame.draw.circle(screen, fill_color, (int(cx), int(cy)), radius)


# ---------------------------------------------------------------
# NOTE GENERATION
# ---------------------------------------------------------------
def _local_median_gap(gaps, idx):
    lo = max(0, idx - LOCAL_MEDIAN_WINDOW)
    hi = min(len(gaps), idx + LOCAL_MEDIAN_WINDOW)
    window = gaps[lo:hi]
    if not window:
        return DEFAULT_LOCAL_GAP_MS
    s = sorted(window)
    m = len(s)
    return s[m // 2] if m % 2 == 1 else (s[m // 2 - 1] + s[m // 2]) / 2


def _world_pos(index, center):
    angle = index * 0.7
    x = center[0] + math.cos(angle) * 150
    y = center[1] + math.sin(angle) * 100
    return x, y


def _catmull_rom_point(p0, p1, p2, p3, t):
    t2 = t * t
    t3 = t2 * t
    x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
               (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
               (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
    y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
               (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
               (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
    return x, y


def smooth_path(points, samples_per_segment=8):
    n = len(points)
    if n < 3:
        return [dict(p) for p in points]

    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    ts = [p["t"] for p in points]
    ext_x = [xs[0]] + xs + [xs[-1]]
    ext_y = [ys[0]] + ys + [ys[-1]]

    result = []
    for k in range(n - 1):
        p0 = (ext_x[k], ext_y[k])
        p1 = (ext_x[k + 1], ext_y[k + 1])
        p2 = (ext_x[k + 2], ext_y[k + 2])
        p3 = (ext_x[k + 3], ext_y[k + 3])
        t_start, t_end = ts[k], ts[k + 1]
        steps = samples_per_segment if k < n - 2 else samples_per_segment + 1
        for s in range(steps):
            frac = s / samples_per_segment
            x, y = _catmull_rom_point(p0, p1, p2, p3, frac)
            t = t_start + (t_end - t_start) * frac
            result.append({"t": int(round(t)), "x": x, "y": y})
    return result


def build_notes_from_grid(onsets_ms, subdivisions, drop_times, center):
    n = len(onsets_ms)
    if n == 0:
        return []

    notes = []
    i = 0
    while i < n:
        t = onsets_ms[i]

        run = [i]
        j = i
        while j + 1 < n and subdivisions[j + 1] in FAST_SUBDIVISIONS:
            run.append(j + 1)
            j += 1

        if subdivisions[i] in FAST_SUBDIVISIONS and len(run) >= FAST_RUN_MIN_LENGTH:
            points = []
            run_is_drop = False
            for idx in run:
                x, y = _world_pos(idx, center)
                points.append({"t": onsets_ms[idx], "x": x, "y": y})
                if any(abs(onsets_ms[idx] - d) < 150 for d in drop_times):
                    run_is_drop = True

            notes.append({
                "type": "hold",
                "points": points,
                "smooth_points": smooth_path(points),
                "is_drop": run_is_drop,
                "judged": False,
                "hold_frames_total": 0,
                "hold_frames_hit": 0,
            })
            i = run[-1] + 1
        else:
            x, y = _world_pos(i, center)
            is_drop = any(abs(t - d) < 150 for d in drop_times)
            notes.append({
                "type": "tap",
                "time_ms": t,
                "x": x, "y": y,
                "is_drop": is_drop,
                "judged": False,
            })
            i += 1

    return notes


def build_notes_from_onsets(onsets_ms, drop_times, center):
    """Fallback for beatmap JSONs without subdivision data."""
    n = len(onsets_ms)
    if n == 0:
        return []

    gaps = [onsets_ms[i + 1] - onsets_ms[i] for i in range(n - 1)]
    notes = []
    i = 0

    while i < n:
        t = onsets_ms[i]

        run = [i]
        j = i
        while j + 1 < n:
            gap = onsets_ms[j + 1] - onsets_ms[j]
            median = _local_median_gap(gaps, j)
            fast_threshold = max(FAST_GAP_FLOOR_MS, FAST_GAP_RATIO * median)
            if gap <= fast_threshold:
                run.append(j + 1)
                j += 1
            else:
                break

        if len(run) >= FAST_RUN_MIN_LENGTH:
            points = []
            run_is_drop = False
            for idx in run:
                x, y = _world_pos(idx, center)
                points.append({"t": onsets_ms[idx], "x": x, "y": y})
                if any(abs(onsets_ms[idx] - d) < 150 for d in drop_times):
                    run_is_drop = True

            notes.append({
                "type": "hold",
                "points": points,
                "smooth_points": smooth_path(points),
                "is_drop": run_is_drop,
                "judged": False,
                "hold_frames_total": 0,
                "hold_frames_hit": 0,
            })
            i = run[-1] + 1
        else:
            x, y = _world_pos(i, center)
            is_drop = any(abs(t - d) < 150 for d in drop_times)
            notes.append({
                "type": "tap",
                "time_ms": t,
                "x": x, "y": y,
                "is_drop": is_drop,
                "judged": False,
            })
            i += 1

    return notes


def note_start_time(note):
    return note["points"][0]["t"] if note["type"] == "hold" else note["time_ms"]


def apply_note_numbers(notes):
    """Small, subtle numbering that resets after each spinner."""
    number = 1
    for note in notes:
        if note["type"] in ("tap", "hold"):
            note["combo_number"] = number
            number += 1
        elif note["type"] == "spinner":
            number = 1
    return notes


def inject_spinners(notes, drop_times, center):
    spinners = []
    for d in drop_times:
        spinners.append({
            "type": "spinner",
            "time_ms": d,
            "end_time_ms": d + SPINNER_DURATION_MS,
            "x": center[0], "y": center[1],
            "is_drop": True,
            "judged": False,
            "total_rotation": 0.0,
            "last_angle": None,
        })

    filtered_notes = []
    for note in notes:
        start = note_start_time(note)
        overlaps = any(sp["time_ms"] <= start <= sp["end_time_ms"] for sp in spinners)
        if not overlaps:
            filtered_notes.append(note)

    combined = filtered_notes + spinners
    combined.sort(key=note_start_time)
    return combined


def load_real_beatmap(audio_path, center):
    base = os.path.splitext(audio_path)[0]
    json_path = base + "_beatmap.json"
    if not os.path.exists(json_path):
        return None, None

    with open(json_path, "r") as f:
        data = json.load(f)

    drop_times = data.get("drops_ms", [])

    if "onsets_ms" not in data:
        print("WARNING: this beatmap JSON has no 'onsets_ms' field - "
              "re-run analyze.py on your song. Falling back to main beats.")
        notes = build_notes_from_onsets(data["beats_ms"], drop_times, center)
    elif "onsets_subdivisions" in data:
        notes = build_notes_from_grid(data["onsets_ms"], data["onsets_subdivisions"], drop_times, center)
    else:
        print("WARNING: this beatmap JSON predates grid-snapping - "
              "re-run analyze.py for osu!-style subdivision timing. Using gap heuristic for now.")
        notes = build_notes_from_onsets(data["onsets_ms"], drop_times, center)

    notes = apply_note_numbers(notes)
    notes = inject_spinners(notes, drop_times, center)

    hold_count = sum(1 for note in notes if note["type"] == "hold")
    spin_count = sum(1 for note in notes if note["type"] == "spinner")
    tap_count = len(notes) - hold_count - spin_count
    bpm = data.get("tempo_bpm")
    print(f"Loaded real beatmap: {tap_count} taps, {hold_count} hold-lines, "
          f"{spin_count} spinners at ~{bpm} BPM")
    return notes, bpm


def generate_synthetic_beatmap(bpm, duration_sec, center):
    interval_ms = int(60000 / bpm)
    onsets_ms = []
    subdivisions = []
    t = 1000
    beat_count = 0
    while t < duration_sec * 1000:
        onsets_ms.append(t)
        subdivisions.append(1)
        beat_count += 1
        if beat_count % 8 == 0:
            onsets_ms.append(t + interval_ms // 4)
            subdivisions.append(4)
            onsets_ms.append(t + 2 * interval_ms // 4)
            subdivisions.append(4)
            onsets_ms.append(t + 3 * interval_ms // 4)
            subdivisions.append(4)
        t += interval_ms

    combined = sorted(zip(onsets_ms, subdivisions))
    onsets_ms = [c[0] for c in combined]
    subdivisions = [c[1] for c in combined]

    fake_drops = [onsets_ms[i] for i in range(0, len(onsets_ms), 40)]
    notes = build_notes_from_grid(onsets_ms, subdivisions, fake_drops, center)
    notes = apply_note_numbers(notes)
    notes = inject_spinners(notes, fake_drops, center)
    print("No real beatmap found - using synthetic pattern with injected 1/4 streams.")
    return notes


# ---------------------------------------------------------------
# PROJECTION
# ---------------------------------------------------------------
def project_point(now_ms, arrival_ms, wx, wy, center):
    time_to_arrival = arrival_ms - now_ms
    travel_ms = BEATS_AHEAD_VISIBLE * 1000
    progress = min(1.0, max(0.0, time_to_arrival / travel_ms))

    z = progress * FOCAL_LENGTH * 3
    scale = FOCAL_LENGTH / (z + FOCAL_LENGTH)

    screen_x = center[0] + (wx - center[0]) * scale
    screen_y = center[1] + (wy - center[1]) * scale
    return screen_x, screen_y, scale, time_to_arrival, progress


def interpolate_along_points(points, now_ms):
    if now_ms <= points[0]["t"]:
        return points[0]["x"], points[0]["y"]
    if now_ms >= points[-1]["t"]:
        return points[-1]["x"], points[-1]["y"]

    for k in range(len(points) - 1):
        a, b = points[k], points[k + 1]
        if a["t"] <= now_ms <= b["t"]:
            span = max(1, b["t"] - a["t"])
            frac = (now_ms - a["t"]) / span
            x = a["x"] + (b["x"] - a["x"]) * frac
            y = a["y"] + (b["y"] - a["y"]) * frac
            return x, y

    return points[-1]["x"], points[-1]["y"]


def judge_timing(tta):
    """Returns (label, base_points, color) or None (miss)."""
    abs_t = abs(tta)
    if abs_t <= HIT_WINDOW_MS * JUDGE_300_FRAC:
        return "300", 3.0, ACCENT
    elif abs_t <= HIT_WINDOW_MS * JUDGE_100_FRAC:
        return "100", 1.0, SUCCESS
    elif abs_t <= HIT_WINDOW_MS:
        return "50", 0.5, WARNING
    return None


def is_combo_milestone(combo):
    return combo in COMBO_MILESTONES or (combo >= 100 and combo % 100 == 0)


# ---------------------------------------------------------------
# MAIN GAME
# ---------------------------------------------------------------
def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
    pygame.display.set_caption("BeaTracer")
    clock = pygame.time.Clock()

    font_score_label = load_font(13)
    font_score_value = load_font(30, bold=True)
    font_acc_label = load_font(13)
    font_acc_value = load_font(22, bold=True)
    font_bpm = load_font(13)
    font_combo_value = load_font(64, bold=True)
    font_combo_label = load_font(14)
    font_note_number = load_font(16, bold=True)
    font_flash = load_font(26, bold=True)
    font_dim = load_font(14)

    center = (SCREEN_W // 2, SCREEN_H // 2)

    use_audio = False
    total_duration_ms = None
    if os.path.exists(AUDIO_PATH):
        try:
            pygame.mixer.init()
            pygame.mixer.music.load(AUDIO_PATH)
            pygame.mixer.music.play()
            use_audio = True
        except Exception as e:
            print(f"Audio failed to load ({e}), falling back to software clock.")
        try:
            snd = pygame.mixer.Sound(AUDIO_PATH)
            total_duration_ms = snd.get_length() * 1000
        except Exception:
            total_duration_ms = None

    start_ticks = pygame.time.get_ticks()

    def get_song_time_ms():
        if use_audio:
            pos = pygame.mixer.music.get_pos()
            if pos >= 0:
                return pos
        return pygame.time.get_ticks() - start_ticks

    notes = None
    bpm_display = FALLBACK_BPM
    if os.path.exists(AUDIO_PATH):
        notes, bpm_loaded = load_real_beatmap(AUDIO_PATH, center)
        if bpm_loaded:
            bpm_display = bpm_loaded
    if notes is None:
        notes = generate_synthetic_beatmap(FALLBACK_BPM, duration_sec=60, center=center)

    if total_duration_ms is None:
        total_duration_ms = max((note_start_time(n) for n in notes), default=60000) + 3000

    score = 0
    misses = 0
    combo = 0
    acc_achieved = 0.0
    acc_possible = 0.0
    flash_texts = []
    combo_pulse_time = -9999

    def add_flash(text, color, pos):
        flash_texts.append((text, color, get_song_time_ms(), pos))

    def register_hit(base_points, max_points, drop_mult):
        nonlocal score, combo, acc_achieved, acc_possible, combo_pulse_time
        combo += 1
        if is_combo_milestone(combo):
            combo_pulse_time = get_song_time_ms()
        mult = 2.0 if combo >= 30 else 1.5 if combo >= 20 else 1.2 if combo >= 10 else 1.0
        score += int(round(base_points * drop_mult * mult))
        acc_achieved += base_points
        acc_possible += max_points

    def register_miss(max_points):
        nonlocal misses, combo, acc_possible
        misses += 1
        combo = 0
        acc_possible += max_points

    running = True
    while running:
        now_ms = get_song_time_ms()
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        screen.fill(BG)

        # faint guide lines previewing the upcoming path
        upcoming = []
        for note in notes:
            if note["type"] == "spinner" or note.get("judged"):
                continue
            ax = note["points"][0]["x"] if note["type"] == "hold" else note["x"]
            ay = note["points"][0]["y"] if note["type"] == "hold" else note["y"]
            st = note_start_time(note)
            sx, sy, _, tta, _ = project_point(now_ms, st, ax, ay, center)
            if -HIT_WINDOW_MS <= tta <= BEATS_AHEAD_VISIBLE * 1000:
                upcoming.append((st, sx, sy))
        upcoming.sort(key=lambda u: u[0])
        for k in range(len(upcoming) - 1):
            _, x1, y1 = upcoming[k]
            _, x2, y2 = upcoming[k + 1]
            pygame.draw.line(screen, BORDER, (x1, y1), (x2, y2), width=1)

        pygame.draw.circle(screen, BORDER, center, TARGET_RADIUS, width=2)

        for note in notes:
            drop_mult = DROP_SCORE_MULT if note["is_drop"] else 1
            accent = ACCENT_SECONDARY if note["is_drop"] else ACCENT

            if note["type"] == "tap":
                sx, sy, scale, tta, progress = project_point(now_ms, note["time_ms"], note["x"], note["y"], center)

                if tta > BEATS_AHEAD_VISIBLE * 1000 or tta < -HIT_WINDOW_MS * 2:
                    continue

                if not note["judged"] and tta < -HIT_WINDOW_MS:
                    note["judged"] = True
                    register_miss(3.0)
                    add_flash("MISS", ERROR, (note["x"], note["y"] - 30))
                    continue

                radius = max(VANISH_SHRINK, TARGET_RADIUS * (DROP_RADIUS_MULT if note["is_drop"] else 1.0) * scale)
                if not note["judged"]:
                    draw_tap_circle(screen, sx, sy, radius, accent)
                    draw_approach_ring(screen, sx, sy, radius, progress, accent)
                    if scale > 0.5:
                        num_surf = font_note_number.render(str(note.get("combo_number", "")), True, TEXT_PRIMARY)
                        screen.blit(num_surf, num_surf.get_rect(center=(int(sx), int(sy))))
                else:
                    pygame.draw.circle(screen, accent, (int(sx), int(sy)), int(radius))

                if not note["judged"] and abs(tta) <= HIT_WINDOW_MS:
                    dist = math.hypot(mouse_pos[0] - sx, mouse_pos[1] - sy)
                    if dist <= radius + HIT_TOLERANCE_PAD:
                        judgement = judge_timing(tta)
                        if judgement:
                            note["judged"] = True
                            label, base_points, jcolor = judgement
                            register_hit(base_points, 3.0, drop_mult)
                            add_flash(label, jcolor, (note["x"], note["y"] - 30))

            elif note["type"] == "hold":
                pts = note["points"]
                start_t = pts[0]["t"]
                end_t = pts[-1]["t"]

                _, _, _, start_tta, start_progress = project_point(now_ms, start_t, pts[0]["x"], pts[0]["y"], center)
                _, _, _, end_tta, _ = project_point(now_ms, end_t, pts[-1]["x"], pts[-1]["y"], center)

                if end_tta > BEATS_AHEAD_VISIBLE * 1000 or end_tta < -(HIT_WINDOW_MS + HOLD_GRACE_MS) * 2:
                    continue

                active = (start_t - HOLD_GRACE_MS) <= now_ms <= (end_t + HOLD_GRACE_MS)
                already_judged = note["judged"]

                projected = [project_point(now_ms, p["t"], p["x"], p["y"], center) for p in pts]
                head_scale = projected[0][2]
                tube_size = max(VANISH_SHRINK * 2, TARGET_RADIUS * 2 * (DROP_RADIUS_MULT if note["is_drop"] else 1.0) * head_scale)

                line_color = accent
                smooth_pts = note.get("smooth_points", pts)
                smooth_projected = [project_point(now_ms, p["t"], p["x"], p["y"], center) for p in smooth_pts]
                for k in range(len(smooth_projected) - 1):
                    sx1, sy1, _, _, _ = smooth_projected[k]
                    sx2, sy2, _, _, _ = smooth_projected[k + 1]
                    if tube_size >= 2:
                        pygame.draw.line(screen, line_color, (sx1, sy1), (sx2, sy2), width=int(tube_size))

                for (sx, sy, sc, _, prog) in projected:
                    draw_pill_node(screen, accent, sx, sy, tube_size)

                if not already_judged:
                    sx0, sy0, sc0, _, _ = projected[0]
                    draw_approach_ring(screen, sx0, sy0, tube_size / 2, start_progress, accent)
                    if sc0 > 0.5:
                        text_color = (14, 15, 18) if luminance(accent) > 150 else TEXT_PRIMARY
                        num_surf = font_note_number.render(str(note.get("combo_number", "")), True, text_color)
                        screen.blit(num_surf, num_surf.get_rect(center=(int(sx0), int(sy0))))

                if active and not already_judged:
                    cx, cy = interpolate_along_points(note.get("smooth_points", pts), now_ms)
                    dist = math.hypot(mouse_pos[0] - cx, mouse_pos[1] - cy)
                    note["hold_frames_total"] += 1
                    if dist <= TARGET_RADIUS + HIT_TOLERANCE_PAD:
                        note["hold_frames_hit"] += 1

                    marker_color = TEXT_PRIMARY if dist <= TARGET_RADIUS + HIT_TOLERANCE_PAD else ERROR
                    pygame.draw.circle(screen, marker_color, (int(cx), int(cy)), 7)

                if not already_judged and now_ms > end_t + HOLD_GRACE_MS:
                    note["judged"] = True
                    total = max(1, note["hold_frames_total"])
                    fraction = note["hold_frames_hit"] / total
                    mid = pts[len(pts) // 2]

                    if fraction >= 0.8:
                        register_hit(3.0, 3.0, drop_mult)
                        add_flash("300", ACCENT, (mid["x"], mid["y"] - 30))
                    elif fraction >= 0.5:
                        register_hit(1.0, 3.0, drop_mult)
                        add_flash("100", SUCCESS, (mid["x"], mid["y"] - 30))
                    else:
                        register_miss(3.0)
                        add_flash("MISS", ERROR, (mid["x"], mid["y"] - 30))

            else:  # spinner
                start_t = note["time_ms"]
                end_t = note["end_time_ms"]
                sx, sy, scale, tta, progress = project_point(now_ms, end_t, note["x"], note["y"], center)

                if tta > BEATS_AHEAD_VISIBLE * 1000 or tta < -HIT_WINDOW_MS * 2:
                    continue

                active = start_t <= now_ms <= end_t
                already_judged = note["judged"]

                ring_radius = max(VANISH_SHRINK, TARGET_RADIUS * 2.2 * scale)
                pygame.draw.circle(screen, ACCENT_SECONDARY, (int(sx), int(sy)), int(ring_radius), width=3)
                if not already_judged and not active:
                    draw_approach_ring(screen, sx, sy, ring_radius, progress, ACCENT_SECONDARY)

                if active and not already_judged:
                    dx = mouse_pos[0] - sx
                    dy = mouse_pos[1] - sy
                    dist_from_center = math.hypot(dx, dy)

                    if dist_from_center >= SPINNER_MIN_RADIUS:
                        angle = math.atan2(dy, dx)
                        if note["last_angle"] is not None:
                            delta = angle - note["last_angle"]
                            while delta > math.pi:
                                delta -= 2 * math.pi
                            while delta < -math.pi:
                                delta += 2 * math.pi
                            note["total_rotation"] += abs(delta)
                        note["last_angle"] = angle
                    else:
                        note["last_angle"] = None

                    rotations = note["total_rotation"] / (2 * math.pi)
                    progress_frac = min(1.0, rotations / SPINNER_REQUIRED_ROTATIONS)
                    spin_label = font_dim.render(f"{rotations:.1f} spins", True, TEXT_MUTED)
                    screen.blit(spin_label, (int(sx) - 30, int(sy) + int(ring_radius) + 10))
                    pygame.draw.arc(
                        screen, ACCENT_SECONDARY,
                        (sx - ring_radius, sy - ring_radius, ring_radius * 2, ring_radius * 2),
                        -math.pi / 2, -math.pi / 2 + progress_frac * 2 * math.pi, width=5
                    )

                if not already_judged and now_ms > end_t:
                    note["judged"] = True
                    rotations = note["total_rotation"] / (2 * math.pi)
                    fraction = min(1.0, rotations / SPINNER_REQUIRED_ROTATIONS)
                    label_pos = (note["x"], note["y"] - ring_radius - 30)

                    if fraction >= 0.8:
                        register_hit(5.0, 5.0, drop_mult)
                        add_flash("300", ACCENT, label_pos)
                    elif fraction >= 0.4:
                        register_hit(2.0, 5.0, drop_mult)
                        add_flash("100", SUCCESS, label_pos)
                    else:
                        register_miss(5.0)
                        add_flash("MISS", ERROR, label_pos)

        pygame.draw.circle(screen, TEXT_PRIMARY, mouse_pos, 5)

        flash_texts[:] = [f for f in flash_texts if now_ms - f[2] < 500]
        for text, color, spawn_time, pos in flash_texts:
            fprogress = (now_ms - spawn_time) / 500
            draw_text_with_shadow(screen, font_flash, text, color,
                                   (pos[0], pos[1] - int(fprogress * 25)), center=True)

        # --- thin single-color progress line, top edge ---
        progress_frac = now_ms / total_duration_ms if total_duration_ms else 0
        width_px = int(SCREEN_W * max(0.0, min(1.0, progress_frac)))
        pygame.draw.rect(screen, BORDER, (0, 0, SCREEN_W, 3))
        if width_px > 0:
            pygame.draw.rect(screen, ACCENT, (0, 0, width_px, 3))

        # --- HUD: top-left SCORE, top-right ACC + BPM, bottom-center COMBO ---
        score_val = font_score_value.render(f"{score:,}", True, TEXT_PRIMARY)
        score_lbl = font_score_label.render("SCORE", True, TEXT_MUTED)
        screen.blit(score_val, (24, 16))
        screen.blit(score_lbl, (24, 16 + score_val.get_height() + 2))

        accuracy = 100.0 if acc_possible == 0 else 100.0 * acc_achieved / acc_possible
        acc_val = font_acc_value.render(f"{accuracy:.2f}%", True, TEXT_PRIMARY)
        acc_lbl = font_acc_label.render("ACC", True, TEXT_MUTED)
        bpm_lbl = font_bpm.render(f"{bpm_display:.0f} BPM" if isinstance(bpm_display, float) else f"{bpm_display} BPM",
                                   True, TEXT_MUTED)
        screen.blit(acc_val, (SCREEN_W - 24 - acc_val.get_width(), 16))
        screen.blit(acc_lbl, (SCREEN_W - 24 - acc_lbl.get_width(), 16 + acc_val.get_height() + 2))
        screen.blit(bpm_lbl, (SCREEN_W - 24 - bpm_lbl.get_width(), 16 + acc_val.get_height() + acc_lbl.get_height() + 8))

        if combo > 0:
            pulse_elapsed = now_ms - combo_pulse_time
            combo_scale = 1.0
            if 0 <= pulse_elapsed < COMBO_PULSE_MS:
                combo_scale = 1.0 + 0.18 * (1 - pulse_elapsed / COMBO_PULSE_MS)

            combo_surf = font_combo_value.render(str(combo), True, ACCENT)
            if combo_scale != 1.0:
                combo_surf = pygame.transform.rotozoom(combo_surf, 0, combo_scale)
            combo_lbl = font_combo_label.render("COMBO", True, TEXT_SECONDARY)

            combo_rect = combo_surf.get_rect(center=(SCREEN_W // 2, SCREEN_H - 70))
            lbl_rect = combo_lbl.get_rect(center=(SCREEN_W // 2, combo_rect.bottom + 12))
            screen.blit(combo_surf, combo_rect)
            screen.blit(combo_lbl, lbl_rect)

        mode_text = font_dim.render(
            "Audio" if use_audio else "No audio found - software clock",
            True, TEXT_MUTED
        )
        screen.blit(mode_text, (16, SCREEN_H - 24))

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()