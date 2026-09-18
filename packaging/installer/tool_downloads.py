"""The mpv and ffmpeg builds the installer fetches, pinned to the byte.

Why fetched and not shipped inside MisterySetup.exe: mpv and ffmpeg are GPL/LGPL
programs with their own source-offer obligations. Downloading them from the
people who built them keeps Mistery out of the business of redistributing them,
and keeps the installer at 76 MB instead of 330 MB.

Why *shared* builds. Measured on this machine, unpacked:

    mpv.exe               (static, shinchiro)      120.3 MB
    ffmpeg.exe            (static, gyan essentials) 97 MB
    ffprobe.exe           (static, gyan essentials) 97 MB
                                                   -------
                                                   314 MB

    mpv.exe               (static — the only mpv)  120.3 MB
    ffmpeg.exe + ffprobe.exe + 7 av*/sw* DLLs      134.1 MB
                                                   -------
                                                   254.4 MB

Sixty megabytes of that saving is ffmpeg.exe and ffprobe.exe each carrying their
own private copy of every codec. There is no shared mpv build — shinchiro ships
one static exe and that is the whole choice — so mpv stays 120 MB.

Why these exact releases, and not "latest":

    BtbN prunes daily FFmpeg autobuilds after about two weeks but keeps the last
    build of each month; the list goes back to autobuild-2024-10-31. So the pin
    is a month-end tag, which should still be there in a year.

    shinchiro keeps only the most recent 30 mpv releases — 20260903 down to
    20260525 on the day this was pinned — so a GitHub URL for it has months, not
    years. SourceForge is the project's own permanent archive and still has
    builds from May, so it is listed as the second URL. Both serve the same
    bytes: the SHA-256 below was checked against a download from each.

Every pin is checked with `python packaging/installer/tool_downloads.py --check`,
which asks the two URLs whether the file is still there and still that size.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolDownload:
    """One archive to fetch, and the handful of files worth keeping out of it.

    `wanted` maps the name the file must end up with in runtime\\ to the path
    inside the archive. The installer writes the key, never the path from the
    archive — a zip entry called "..\\..\\Windows\\System32\\evil.dll" cannot
    become a filename because no filename ever comes out of the archive.
    """

    name: str
    archive_name: str
    size: int
    sha256: str
    urls: tuple[str, ...]
    wanted: dict[str, str] = field(default_factory=dict)
    homepage: str = ""
    license_note: str = ""
    unpacked_size: int = 0          # measured, for the "needs N MB" line


MPV = ToolDownload(
    name="mpv",
    archive_name="mpv-x86_64-20260830-git-e8673660ab.7z",
    size=33_752_310,
    sha256="464ab69b2248e7b592f0c27a927ffd1f016f7fa2d6d8b46b1a98254c5f2b670a",
    urls=(
        "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/"
        "20260830/mpv-x86_64-20260830-git-e8673660ab.7z",
        "https://sourceforge.net/projects/mpv-player-windows/files/64bit/"
        "mpv-x86_64-20260830-git-e8673660ab.7z/download",
    ),
    # mpv.com is a 3.5 KB console shim Mistery never calls, and
    # d3dcompiler_43.dll is mpv's fallback for machines older than Windows 8:
    # mpv asks for d3dcompiler_47, _46, then _43, and Windows 11 has _47 in
    # System32 (4.7 MB, checked). Leaving both out saves 4.5 MB.
    wanted={"mpv.exe": "mpv.exe"},
    homepage="https://mpv.io/installation/",
    license_note="mpv — GPLv2+ (built by shinchiro)",
    unpacked_size=120_330_240,
)

FFMPEG = ToolDownload(
    name="ffmpeg",
    archive_name="ffmpeg-n9.0.1-11-ge47273f4d9-win64-lgpl-shared-9.0.zip",
    size=67_201_333,
    sha256="83a824f0729a69d143c9865125bb86988a11dd388325f0033711045522068aa0",
    urls=(
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
        "autobuild-2026-08-31-13-27/"
        "ffmpeg-n9.0.1-11-ge47273f4d9-win64-lgpl-shared-9.0.zip",
    ),
    # The LGPL build, not the GPL one: Mistery only ever decodes and probes
    # (app/metadata/artwork.py, thumbs.py, introdetect.py), so x264 and x265 —
    # the encoders that make the GPL build GPL — are 9 MB of download for
    # something nothing calls.
    #
    # ffplay.exe (18.1 MB) is left behind for the same reason. Both exes import
    # all seven libraries (checked with pefile), so all seven come along.
    wanted={
        "ffmpeg.exe": "bin/ffmpeg.exe",
        "ffprobe.exe": "bin/ffprobe.exe",
        "avcodec-63.dll": "bin/avcodec-63.dll",
        "avdevice-63.dll": "bin/avdevice-63.dll",
        "avfilter-12.dll": "bin/avfilter-12.dll",
        "avformat-63.dll": "bin/avformat-63.dll",
        "avutil-61.dll": "bin/avutil-61.dll",
        "swresample-7.dll": "bin/swresample-7.dll",
        "swscale-10.dll": "bin/swscale-10.dll",
    },
    homepage="https://github.com/BtbN/FFmpeg-Builds",
    license_note="FFmpeg — LGPLv2.1+ (built by BtbN)",
    unpacked_size=134_069_248,
)

TOOLS: tuple[ToolDownload, ...] = (MPV, FFMPEG)

# What the two downloads and their unpacked files add up to, for the disk-space
# check and the line the installer shows before it starts.
DOWNLOAD_BYTES = sum(t.size for t in TOOLS)              # 100.95 MB
RUNTIME_BYTES = sum(t.unpacked_size for t in TOOLS)      # 254.4 MB


def _check(timeout: int = 30) -> int:
    """Ask every URL whether the file is still there and still that size.

    Upstreams prune old releases. This is the two-second check to run before
    cutting a release, so that a stale pin is found here and not by somebody
    installing Mistery for the first time.
    """
    import urllib.error
    import urllib.request

    problems = 0
    for tool in TOOLS:
        print(f"{tool.name}  {tool.archive_name}  {tool.size} bytes")
        for url in tool.urls:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mistery-Setup/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    length = response.headers.get("Content-Length")
                    ok = length is not None and int(length) == tool.size
                    print(f"    {'ok  ' if ok else 'SIZE'} {length or '?':>10}  {url}")
                    problems += 0 if ok else 1
            except (urllib.error.URLError, OSError, ValueError) as error:
                print(f"    GONE            {url}\n         {error}")
                problems += 1
    print("\nall pins reachable" if not problems else f"\n{problems} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        raise SystemExit(_check())
    for _tool in TOOLS:
        print(f"{_tool.name:8s} {_tool.size / 1e6:6.1f} MB download  "
              f"{_tool.unpacked_size / 1e6:6.1f} MB unpacked  {_tool.sha256}")
    print(f"{'total':8s} {DOWNLOAD_BYTES / 1e6:6.1f} MB download  "
          f"{RUNTIME_BYTES / 1e6:6.1f} MB unpacked")
