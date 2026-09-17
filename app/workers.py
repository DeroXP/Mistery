"""Background library pipeline: scan → probe → metadata → thumbnails.

Everything runs off the UI thread on a QThreadPool. Stages are sequential and
in dependency order (metadata needs the runtime from probing, thumbnails need
both), and each stage is interruptible so quitting the app is instant.
"""

from __future__ import annotations

import collections
import os
import threading
import time
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from . import db, parser, probe, scanner
from .config import settings
from .metadata import artwork, online, thumbs
from .metadata.tmdb import TmdbClient, TmdbError
from .music import library as music_library

_ONLINE_SOURCES = ("tmdb", "tvmaze", "wikipedia")

# Lookups the UI asks for are about what is on screen: each song change asks
# for that song's lyrics and the next one's. Anything queued before those two is
# for a song already skipped.
_LOOKUP_BACKLOG = 2


class _PipelineTask(QRunnable):
    def __init__(self, service: "LibraryService", force: bool, scan: bool) -> None:
        super().__init__()
        self._service = service
        self._force = force
        self._scan = scan
        self._dead = False

    def _emit(self, signal, *args) -> None:
        """Emit unless the service has already been torn down.

        On quit the C++ side of LibraryService can be destroyed while this task
        is still winding down; emitting then raises RuntimeError.
        """
        if self._dead:
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            self._dead = True

    @property
    def _stop(self) -> bool:
        """True when work should end. Blocks while the service is paused.

        Every stage loop already checks this, so pausing during playback needs
        no extra plumbing.
        """
        if self._dead or self._service.cancelled:
            return True
        self._service.wait_while_paused()
        return self._service.cancelled

    def _until_paused(self, work) -> dict:
        """Run one ffmpeg job so that a pause stops it at once, not at the end.

        The stage loops only notice a pause between files, but a thumbnail pass
        on a 4K film is 13–23 s of decoding on the GPU mpv is about to use, and
        a poster takes up to four frame grabs of a minute each — the very start
        of the film the pause is for. So `work` gets a cancel callback that also
        answers yes to a pause; the job is killed, and once the pause is over the
        same file is started again, because a job cut short returns 'pending'
        and would otherwise wait for the next pass.
        """
        service = self._service
        while True:
            interrupted = False

            def cancel() -> bool:
                nonlocal interrupted
                if self._dead or service.cancelled:
                    return True
                if service.paused:
                    interrupted = True
                    return True
                return False

            fields = work(cancel)
            if not interrupted or service.cancelled:
                return fields
            service.wait_while_paused()
            if service.cancelled:
                return fields

    def run(self) -> None:
        service = self._service
        try:
            self._emit(service.busy_changed, True)

            # Parsing rules changed since this library was built: re-derive
            # titles and show grouping before anything else looks at them.
            stored = int(settings.get("parser_version", 0) or 0)
            if stored != parser.PARSER_VERSION and not self._stop:
                self._emit(service.status, "Updating titles…")
                fixed = scanner.reparse_names()
                settings.set("parser_version", parser.PARSER_VERSION)
                if fixed:
                    self._emit(service.library_changed)

            if self._scan and not self._stop:
                self._emit(service.status, "Scanning library…")
                result = scanner.scan(force=self._force)
                self._emit(service.scan_finished, result)
                if result.changed:
                    self._emit(service.library_changed)

            # Music before the video stages: those can run for many minutes
            # (thumbnails, intro detection) and a new album shouldn't wait on them.
            if self._scan and not self._stop:
                self._music_stage()

            if not self._stop:
                self._probe_stage()
            if not self._stop:
                self._metadata_stage()
            if not self._stop and settings.get("generate_thumbs", True):
                self._thumbs_stage()
            if not self._stop and settings.get("detect_intros", True):
                self._tv_stage()

            if not self._stop:
                self._emit(service.status, "")
        except Exception:
            self._emit(service.status, "Library update failed")
            self._emit(service.error, traceback.format_exc(limit=6))
        finally:
            self._emit(service.busy_changed, False)
            db.close_thread_connection()

    # --- stages -------------------------------------------------------------

    def _music_stage(self) -> None:
        service = self._service
        if service._music_running:
            return                      # the download watcher is mid-pass already
        self._emit(service.status, "Scanning music…")
        result = music_library.scan(settings.library_folders(), force=self._force,
                                    cancel=lambda: self._stop)
        built = 0
        if not self._stop:
            built = music_library.build_artwork(cancel=lambda: self._stop)
        if result.changed or built:
            self._emit(service.music_changed)
        if not self._stop:
            def note(name: str, index: int, total: int) -> None:
                self._emit(service.status, f"Measuring loudness ({index}/{total}) — {name}")
            if music_library.measure_loudness(cancel=lambda: self._stop, progress=note):
                self._emit(service.music_changed)

    def _probe_stage(self) -> None:
        service = self._service
        pending = db.pending("probe_state", limit=5000)
        for index, row in enumerate(pending, start=1):
            if self._stop:
                return
            self._emit(service.status,
                       f"Reading media info ({index}/{len(pending)}) — {row['title']}")
            result = probe.probe(row["path"])
            db.update_media(row["id"], **probe.to_media_fields(result))
            self._emit(service.media_updated, int(row["id"]))
        if pending:
            self._emit(service.library_changed)

    def _metadata_stage(self) -> None:
        """Source chain: TMDB (with a key) → keyless online → frames from the file."""
        service = self._service
        client = TmdbClient(settings.get("tmdb_api_key", ""), settings.get("tmdb_language", "en-US"))
        tmdb_broken = False
        online_broken = False

        for show in db.pending_show_metadata(limit=500):
            if self._stop:
                return
            fields = None
            # "No match" only means something when every source that could have
            # matched was actually asked and answered.
            answered = True
            self._emit(service.status, f"Looking up {show['title']}…")
            if client.enabled:
                if tmdb_broken:
                    answered = False
                else:
                    try:
                        fields = client.show_fields(show["title"], show["year"])
                    except TmdbError as exc:
                        tmdb_broken = True
                        answered = False
                        self._emit(service.status, f"TMDB unavailable: {exc}")
            if not fields:
                if online_broken:
                    answered = False
                else:
                    try:
                        fields = online.tvmaze_show(show["title"], show["year"])
                    except online.OnlineError as exc:
                        online_broken = True
                        answered = False
                        self._emit(service.status, f"Online lookup unavailable: {exc}")
            if fields:
                db.update_show(int(show["id"]), **fields)
            elif answered:
                db.update_show(int(show["id"]), meta_state="fallback")
            else:
                # Offline, or the source was failing: not an answer. Writing
                # 'fallback' here was for good — shows in that state are never
                # looked up again, so the show got no TVmaze id and its episodes
                # stayed "Episode 1, Episode 2" with frame grabs however long
                # the network had been back. Left 'pending', the next pass asks.
                continue
            self._emit(service.show_updated, int(show["id"]))

        pending = db.pending("meta_state", limit=5000)
        for index, row in enumerate(pending, start=1):
            if self._stop:
                return
            # An outage means we cannot improve on what is already there, so
            # stop rather than re-cutting artwork we already have.
            if tmdb_broken and online_broken and row["meta_state"] == "fallback":
                continue
            title = row["title"]
            fields: dict | None = None
            self._emit(service.status,
                       f"Fetching details ({index}/{len(pending)}) — {title}")

            show = db.get_show(row["show_id"]) if row["show_id"] else None
            if client.enabled and not tmdb_broken:
                try:
                    if row["kind"] == "movie":
                        fields = client.movie_fields(title, row["year"])
                    elif show and show["tmdb_id"]:
                        fields = client.episode_fields(
                            int(show["tmdb_id"]), row["season"] or 1, row["episode"] or 1
                        )
                except TmdbError as exc:
                    tmdb_broken = True
                    self._emit(service.status, f"TMDB unavailable: {exc}")

            if not fields and not online_broken:
                try:
                    if row["kind"] == "movie":
                        fields = online.wikipedia_movie(title, row["year"])
                    elif show and show["tvmaze_id"]:
                        fields = online.tvmaze_episode_fields(
                            int(show["tvmaze_id"]), row["season"] or 1, row["episode"] or 1
                        )
                except online.OnlineError as exc:
                    online_broken = True
                    self._emit(service.status, f"Online lookup unavailable: {exc}")

            if not fields and row["meta_source"] in _ONLINE_SOURCES:
                # This row was matched online before and is being looked up
                # again (a refetch from Settings). Finding nothing now, or not
                # reaching anyone, must not undo that: cut art used to replace
                # exactly the real poster and still, and the row was marked done
                # so they never came back. Keep what is there, fill only art the
                # row lacks, and stay queued if the sources were unreachable.
                fields = {}
                lacking = [key for key in ("poster", "backdrop") if not row[key]]
                if lacking:
                    generated = self._generate_art(row)
                    if generated.get("meta_state") == "pending":
                        continue            # cut short by quitting; still queued
                    fields = {key: generated[key] for key in lacking if generated.get(key)}
                if not ((client.enabled and tmdb_broken) or online_broken):
                    fields["meta_state"] = "done"
                if fields:
                    db.update_media(int(row["id"]), **fields)
                    self._emit(service.media_updated, int(row["id"]))
                continue

            # Fill whatever is still missing from the file itself — and always
            # give movies a real backdrop, which the online sources lack.
            if not fields or (row["kind"] == "movie" and not fields.get("backdrop")):
                generated = self._generate_art(row)
                if fields:
                    # Online data won — keep its state, borrow only the art.
                    if generated.get("backdrop"):
                        fields["backdrop"] = generated["backdrop"]
                    if not fields.get("poster") and generated.get("poster"):
                        fields["poster"] = generated["poster"]
                else:
                    fields = generated

            db.update_media(int(row["id"]), **fields)
            self._emit(service.media_updated, int(row["id"]))

        if pending:
            self._emit(service.library_changed)

    def _generate_art(self, row) -> dict:
        return self._until_paused(lambda cancel: artwork.generate(
            row["path"], row["duration"] or 0, row["hdr"], cancel=cancel,
        ))

    def _tv_stage(self) -> None:
        """Learn intro and credits positions by matching audio across a season."""
        service = self._service
        try:
            from .metadata import introdetect
        except ImportError:
            # numpy missing — detection is optional, everything else still works
            return
        for show_id, season in db.seasons_needing_tv_analysis():
            if self._stop:
                return
            episodes = [dict(r) for r in db.season_episodes(show_id, season)]
            show = db.get_show(show_id)
            label = show["title"] if show else "show"
            self._emit(service.status,
                       f"Learning intros — {label} season {season}…")
            results = introdetect.analyze_season(
                episodes,
                cancel=lambda: self._stop,
                progress=lambda msg: self._emit(
                    service.status, f"Learning intros — {label} S{season:02d}: {msg}"
                ),
            )
            if self._stop:
                return
            for episode in episodes:
                media_id = int(episode["id"])
                fields = dict(results.get(media_id) or {})
                fields["tv_state"] = "done"
                db.update_media(media_id, **fields)
            found = sum(1 for r in results.values() if "intro_end" in r)
            self._emit(service.status,
                       f"{label} S{season:02d}: intros found for "
                       f"{found}/{len(episodes)} episode(s)")
            self._emit(service.library_changed)

    def _thumbs_stage(self) -> None:
        service = self._service
        pending = db.pending("thumbs_state", limit=5000)
        count = int(settings.get("thumb_count", 120))
        for index, row in enumerate(pending, start=1):
            if self._stop:
                return
            if not row["duration"]:
                db.update_media(int(row["id"]), thumbs_state="error")
                continue
            self._emit(
                service.status,
                f"Building preview thumbnails ({index}/{len(pending)}) — {row['title']}",
            )
            fields = self._until_paused(lambda cancel: thumbs.generate(
                row["path"], row["duration"], row["hdr"], count=count, cancel=cancel,
            ))
            db.update_media(int(row["id"]), **fields)
            self._emit(service.media_updated, int(row["id"]))


class _CallableTask(QRunnable):
    """Run one function off the UI thread and report it back."""

    def __init__(self, service: "LibraryService", func, on_done=None) -> None:
        super().__init__()
        self._service = service
        self._func = func
        self._on_done = on_done

    def run(self) -> None:
        try:
            result = self._func()
        except Exception:
            try:
                self._service.error.emit(traceback.format_exc(limit=6))
            except RuntimeError:
                pass
            return
        finally:
            db.close_thread_connection()
        if self._on_done is not None and not self._service.closed:
            try:
                self._service.task_done.emit(self._on_done, result)
            except RuntimeError:
                pass


class LibraryService(QObject):
    """Owns the background pipeline and reports progress to the UI."""

    status = Signal(str)
    error = Signal(str)
    busy_changed = Signal(bool)
    scan_finished = Signal(object)
    library_changed = Signal()
    media_updated = Signal(int)
    show_updated = Signal(int)
    music_changed = Signal()
    task_done = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(2)
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._running = False
        self._music_running = False
        self._closed = False
        self._lookups: collections.deque[_CallableTask] = collections.deque(maxlen=_LOOKUP_BACKLOG)
        self._lookups_ready = threading.Condition()
        self._lookup_thread: threading.Thread | None = None
        self.busy_changed.connect(self._track_busy)
        self.task_done.connect(self._deliver)

    # --- pause ---------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """Hold background work, e.g. while a film is playing."""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def wait_while_paused(self, poll: float = 0.25) -> None:
        while self._paused.is_set() and not self._cancel.is_set():
            time.sleep(poll)

    def _track_busy(self, busy: bool) -> None:
        self._running = busy

    def _deliver(self, callback, result) -> None:
        """Hand a background result to its callback, here on the GUI thread.

        The result comes through a queued signal, so it can arrive after
        shutdown() even though the task checked `closed` when it sent it. A
        download-watcher result that did started a new pipeline on the way
        out (see refresh), and a lyrics result would update a window that is
        being torn down.
        """
        if not self._closed:
            callback(result)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def busy(self) -> bool:
        return self._running

    def refresh(self, force: bool = False, scan: bool = True) -> None:
        # After shutdown the cancel flag is what keeps work from running;
        # clearing it here would start a whole pipeline while the app quits.
        if self._running or self._closed:
            return
        self._cancel.clear()
        self._running = True
        self._pool.start(_PipelineTask(self, force=force, scan=scan))

    def run_async(self, func, on_done=None) -> None:
        """Run a quick lookup for the UI (lyrics) off its thread, and report back.

        Lookups get a thread of their own instead of a place in the pipeline's
        pool. On a network that connects but never answers, one lyrics lookup
        takes about half a minute; two of them filled that pool, so a finished
        download waited over a minute to appear, the song on screen waited
        behind lookups for songs already skipped, and quitting sat out the
        whole eight-second shutdown wait. The thread is a daemon, so quitting
        does not wait for it at all — a lookup only reads the network and caches
        its answer, and SQLite keeps the database whole if the process ends
        halfway through one.

        Only the newest requests are kept (see _LOOKUP_BACKLOG): anything older
        is for a song the user has moved past, and running it would only delay
        the one they are listening to.
        """
        with self._lookups_ready:
            if self._closed:
                return
            self._lookups.append(_CallableTask(self, func, on_done))
            self._lookups_ready.notify()
            if self._lookup_thread is None:
                self._lookup_thread = threading.Thread(
                    target=self._run_lookups, name="mistery-lookups", daemon=True)
                self._lookup_thread.start()

    def _run_lookups(self) -> None:
        while True:
            with self._lookups_ready:
                while not self._lookups and not self._closed:
                    self._lookups_ready.wait()
                if self._closed:
                    return
                task = self._lookups.popleft()
            task.run()

    def refresh_music(self) -> None:
        """The download watcher: a pass that finishes downloads while the app is open.

        Cheap enough to run on a timer — a walk, a stat per file, and a real read
        only for new files and the tracks still marked as downloading. Videos
        that were still downloading get one ffprobe each: nothing else looks at
        them until a rescan or the next start, so a film that finished while the
        app was open stayed hidden and "still downloading" until then.

        It keeps to the same pause as the pipeline. Loudness measuring is a full
        decode per song, and a watcher pass that was already running kept doing
        it for an album's worth of 24/96 FLACs while a film played or a game ran
        with Mistery in the tray.
        """
        if self._running or self._music_running or self._closed:
            return
        self._music_running = True

        def stop() -> bool:
            self.wait_while_paused()
            return self.cancelled

        def work():
            try:
                result = music_library.scan(settings.library_folders(), cancel=stop)
                built = music_library.build_artwork(cancel=stop)
                measured = music_library.measure_loudness(cancel=stop)
                finished = 0 if stop() else _finish_video_downloads(stop)
                return bool(result.changed or built or measured), finished
            finally:
                self._music_running = False

        def done(outcome) -> None:
            music, videos = outcome
            if music:
                self.music_changed.emit()
            if videos:
                self.library_changed.emit()
                # Artwork, details, thumbnails and intros for what just arrived.
                self.refresh(scan=False)

        self._pool.start(_CallableTask(self, work, done))

    def cancel(self) -> None:
        self._cancel.set()

    def shutdown(self, timeout_ms: int = 8000) -> None:
        """Stop background work and wait for it, so nothing outlives the UI."""
        self._cancel.set()
        self._paused.clear()          # let any paused stage notice the cancel
        online.close()                # a failed lookup is not retried on the way out
        with self._lookups_ready:
            self._closed = True
            self._lookups.clear()
            self._lookups_ready.notify_all()
        self._pool.clear()
        self._pool.waitForDone(timeout_ms)


def _finish_video_downloads(stop) -> int:
    """Re-probe videos marked as still downloading; returns how many now aren't."""
    finished = 0
    rows = db.query("SELECT id, path FROM media WHERE probe_state = 'incomplete' AND missing = 0")
    for row in rows:
        if stop():
            break
        fields = probe.to_media_fields(probe.probe(row["path"]))
        if fields.get("probe_state") == "incomplete":
            continue
        try:
            stat = os.stat(row["path"])
        except OSError:
            continue
        # The size and time the file finished with, so the next scan sees the
        # file it was probed as rather than a changed one to start over on.
        fields.update(size=stat.st_size, mtime=stat.st_mtime)
        db.update_media(int(row["id"]), **fields)
        finished += 1
    return finished
