"""The sound knobs: volume matching, bass, clarity and 3D, as one mpv filter chain.

Everything here is a libavfilter graph handed to mpv, which can swap it while a
song plays, so a switch takes effect on the beat rather than on the next track.
With every effect off and nothing to correct there is no graph at all, and mpv
does no extra work.

Three rules shape the design.

*All gain lives in one place.* The graph starts with the decibels that song
needs to reach the target level (see loudness.py) and ends with the limiter, so
nothing can change the level after the limiter has protected it. mpv applies
the graph per file, the same way it applies ReplayGain, which keeps an album
gapless and stops a song starting at the previous song's volume.

*Effects must not smuggle in loudness.* Anything louder sounds better for ten
seconds, so each effect is levelled, by numbers measured on real songs rather
than guessed: switching 3D on costs 0.0 to 0.8 LU, which is nothing. A bass
shelf cannot be free — these records are mastered with no headroom at all, so
bass added on top of a wall has to come out of somewhere — and half from the
level, half from the limiter measured as the cleanest split.

*The limiter only appears when the file needs it.* Every measured song's true
peak is known, so the graph can tell whether this song, with these settings,
can actually reach full scale. When it cannot, the limiter is left out and the
audio reaches the sound card untouched.
"""

from __future__ import annotations

from ..config import settings

# --- what the settings mean ---------------------------------------------------

# Low shelf, in decibels at the bottom end.
BASS_LEVELS: dict[str, float] = {"off": 0.0, "warm": 3.0, "deep": 6.0, "massive": 9.0}
BASS_LABELS = {"off": "Off", "warm": "Warm", "deep": "Deep", "massive": "Massive"}

# 3D: widen the stereo picture, then put one channel a few milliseconds behind
# the other. Per strength: (side gain, delay in ms, make-up gain in dB, worst
# case peak rise in dB). The last two were measured over six mixes.
#
# The first version used ffmpeg's Haas enhancer, which rebuilds both channels
# out of a delayed copy of the middle of the mix plus the sides. That cancels
# frequencies *inside* each channel: measured against the original, one ear's
# spectrum rippled by 3.3–5.0 dB with dips as deep as −24 dB — voices and drums
# audibly losing pieces of themselves. This way nothing is subtracted; each ear
# still gets the whole mix, one of them slightly later. Same measurement: 0.9 to
# 1.2 dB of ripple, and all of that is the widening changing the balance rather
# than notches.
SPATIAL_LEVELS: dict[str, tuple[float, float, float, float]] = {
    "off": (0.0, 0.0, 0.0, 0.0),
    "subtle": (1.6, 5.0, 1.0, 3.2),
    "normal": (2.4, 9.0, 2.5, 6.6),
    "wide": (3.0, 14.0, 3.6, 8.5),
}
SPATIAL_LABELS = {"off": "Off", "subtle": "Subtle", "normal": "Normal", "wide": "Wide"}

BOOST_LEVELS: dict[str, float] = {"off": 0.0, "low": 3.0, "high": 6.0}
BOOST_LABELS = {"off": "Off", "low": "+3 dB", "high": "+6 dB"}

# Where the shelves sit. 95 Hz is kick drum and bass guitar without muddying the
# voice; 8 kHz is where "clarity" lives — cymbals, consonants, air.
_BASS_HZ = 95
_BASS_WIDTH = 0.55
_TREBLE_DB = 3.0
_TREBLE_HZ = 8000
_TREBLE_WIDTH = 0.6

# −1 dBFS, with a short lookahead. level=disabled matters: left on, the filter
# normalises its output back up to full scale, which would undo the volume
# matching and make every song loud again.
_LIMITER = "alimiter=limit=0.891:attack=5:release=60:level=disabled"
PEAK_CEILING = -1.0


def _choice(key: str, table: dict, default: str) -> str:
    value = str(settings.get(key, default) or default).lower()
    return value if value in table else default


def current() -> dict:
    """Every sound setting, normalised to values these tables know."""
    return {
        "bass": _choice("music_bass", BASS_LEVELS, "off"),
        "clarity": bool(settings.get("music_clarity", False)),
        "spatial": _choice("music_spatial", SPATIAL_LEVELS, "off"),
        "boost": _choice("music_boost", BOOST_LEVELS, "off"),
    }


def is_active(state: dict | None = None) -> bool:
    """Is any effect switched on? (Volume matching on its own is not an effect.)"""
    state = state or current()
    return bool(BASS_LEVELS[state["bass"]] or state["clarity"]
                or SPATIAL_LEVELS[state["spatial"]][0] or BOOST_LEVELS[state["boost"]])


def headroom(state: dict | None = None) -> float:
    """Decibels the effects give back so their own boosts have room to breathe."""
    state = state or current()
    room = BASS_LEVELS[state["bass"]] * 0.5
    if state["clarity"]:
        room += _TREBLE_DB * 0.5      # a shelf, like the bass one
    # Widening makes a mix louder by however much of it was already at the
    # sides, so 3D gives back its measured average — it must sound different,
    # not bigger, or an A/B comparison means nothing.
    room += SPATIAL_LEVELS[state["spatial"]][2]
    return room


def peak_rise(state: dict | None = None) -> float:
    """Worst case decibels these effects can add to a file's true peak."""
    state = state or current()
    rise = BASS_LEVELS[state["bass"]]
    if state["clarity"]:
        rise += _TREBLE_DB
    rise += SPATIAL_LEVELS[state["spatial"]][3]
    return rise


def level_db(state: dict | None = None, gain_db: float = 0.0) -> float:
    """The single gain at the head of the chain: matching, plus boost, less headroom."""
    state = state or current()
    return round(gain_db + BOOST_LEVELS[state["boost"]] - headroom(state), 2)


def chain(state: dict | None = None, gain_db: float = 0.0,
          peak_db: float | None = None) -> str:
    """The value for mpv's ``af`` property for one file.

    `gain_db` is that file's volume-matching gain and `peak_db` its measured
    true peak. With nothing switched on and no gain to apply, the answer is
    empty and mpv runs no filters at all.
    """
    state = state or current()
    stages: list[str] = []

    level = level_db(state, gain_db)
    if abs(level) >= 0.05:
        stages.append(f"volume={level:g}dB")

    bass_db = BASS_LEVELS[state["bass"]]
    if bass_db:
        stages.append(f"bass=g={bass_db:g}:f={_BASS_HZ}:w={_BASS_WIDTH}")
    if state["clarity"]:
        stages.append(f"treble=g={_TREBLE_DB:g}:f={_TREBLE_HZ}:w={_TREBLE_WIDTH}")

    width, delay_ms = SPATIAL_LEVELS[state["spatial"]][:2]
    if width:
        # aformat first: a mono file has no sides to widen, and the widener
        # refuses to run on one rather than passing it through.
        stages.append("aformat=channel_layouts=stereo")
        stages.append(f"extrastereo=m={width:g}:c=0")     # c=0: our limiter, not its clipper
        stages.append(f"adelay=0|{delay_ms:g}")

    if not stages:
        return ""
    if _clipping_risk(state, level, peak_db):
        stages.append(_LIMITER)
    return "lavfi=[" + ",".join(stages) + "]"


def _clipping_risk(state: dict, level: float, peak_db: float | None) -> bool:
    """Could this file, with these settings, reach full scale?

    An unmeasured file has no known peak, so anything that can lift peaks is
    treated as a risk. A measured one is judged on its own loudest moment.
    """
    rise = peak_rise(state)
    if peak_db is None:
        return rise > 0 or level > 0
    return peak_db + level + rise > PEAK_CEILING


def describe(state: dict | None = None) -> str:
    """One line for the interface: what is switched on right now."""
    state = state or current()
    parts = []
    if BASS_LEVELS[state["bass"]]:
        parts.append(f"{BASS_LABELS[state['bass']].lower()} bass")
    if state["clarity"]:
        parts.append("clarity")
    if SPATIAL_LEVELS[state["spatial"]][0]:
        parts.append(f"3D {SPATIAL_LABELS[state['spatial']].lower()}")
    if BOOST_LEVELS[state["boost"]]:
        parts.append(f"{BOOST_LABELS[state['boost']]} louder")
    return ", ".join(parts) if parts else "Off"
