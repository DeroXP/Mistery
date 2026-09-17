"""Small formatting helpers shared by the UI."""

from __future__ import annotations


def fmt_duration(seconds: float | None) -> str:
    """1h 47m / 47m / 0m — for runtimes."""
    if not seconds or seconds <= 0:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def fmt_clock(seconds: float | None) -> str:
    """1:47:23 / 47:23 — for playback position."""
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_size(num_bytes: float | None) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def fmt_remaining(position: float, duration: float) -> str:
    left = max(0.0, (duration or 0) - (position or 0))
    return f"{fmt_duration(left)} left" if left >= 60 else "Almost done"


def progress_fraction(position: float | None, duration: float | None) -> float:
    if not duration or duration <= 0:
        return 0.0
    return max(0.0, min(1.0, (position or 0) / duration))


def episode_code(season: int | None, episode: int | None) -> str:
    if season is None or episode is None:
        return ""
    return f"S{season:02d}E{episode:02d}"


def reveal_in_explorer(path) -> None:
    """Open the containing folder with the file selected."""
    import subprocess
    import sys
    from pathlib import Path

    from .config import subprocess_flags

    target = Path(path)
    if not target.exists() and not target.parent.is_dir():
        return
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)],
                             creationflags=subprocess_flags())
    except OSError:
        pass


def elide(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
