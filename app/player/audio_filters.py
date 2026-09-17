"""Audio filter chains applied to mpv at runtime via the `af` property."""

from __future__ import annotations

# "Dialogue boost" / night mode.
#
# These files carry EAC3 5.1 mastered for a cinema: dialogue sits ~20 dB below
# the action peaks. A compressor pulls the loud material down and makes up the
# gain, so speech becomes audible without the explosions clipping. The limiter
# is a safety net for the makeup gain.
DIALOGUE_BOOST_CHAIN = (
    "lavfi=[acompressor="
    "threshold=0.045:"      # ≈ -27 dBFS, sits just under conversational level
    "ratio=6:"
    "attack=15:"
    "release=400:"
    "makeup=3.2:"
    "knee=6,"
    "alimiter=limit=0.94:level=disabled]"
)

# Applied when downmixing 5.1 to stereo speakers: the centre channel carries
# nearly all the dialogue, so it gets lifted relative to the surrounds.
CENTRE_LIFT_CHAIN = (
    "lavfi=[pan=stereo|"
    "FL=0.9*FC+0.8*FL+0.5*SL+0.4*LFE|"
    "FR=0.9*FC+0.8*FR+0.5*SR+0.4*LFE]"
)


def build_chain(dialogue_boost: bool, downmix_centre_lift: bool = False) -> str:
    """Compose the `af` value for the current toggles ("" clears all filters)."""
    parts = []
    if downmix_centre_lift:
        parts.append(CENTRE_LIFT_CHAIN)
    if dialogue_boost:
        parts.append(DIALOGUE_BOOST_CHAIN)
    return ",".join(parts)
