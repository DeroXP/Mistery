# Mistery

A local movie and TV library with a proper video player built in — for files
already on your disk. Dark, cinematic UI; mpv doing the decoding.

```bash
python main.py
```

Or use `Mistery.bat` / the Start Menu entry, both of which launch via `pythonw.exe`
so no console window appears.

## Checking everything works

```bash
python selfcheck.py
```

Inspects tools, database integrity, library health, artwork and sprite coverage,
and runs the filename-parser regression cases. `--fix` also queues anything that
failed for another attempt.

```bash
python tools\repair_db.py
```

Rebuilds the database if SQLite ever reports `database disk image is malformed`.
Salvages every readable row of every table into a fresh file, keeps the damaged
one (with its `-wal` and `-shm`) as a `.corrupt-<timestamp>.db` backup, drops the
regenerable HTTP cache, and reports exactly what was saved. When anything was
lost it says what and exits with code 2: keep the backup then. A database with
no damage is left alone. Refuses to run while the app is open; Mistery offers
the same repair itself when it cannot open a damaged library.

```bash
python smoketest.py
```

Launches the interface and actually plays video — every page, a movie, an
episode, the autoplay chain, hardware decoding, dropped frames, dialogue boost,
VR detection and shutdown time, plus music: an album, a whole discography queued
at once, the Sound panel and the tray. Plays muted, and works on a **copy** of
your library in a temporary folder, so nothing it scans, builds or plays touches
the real one (`--live` runs it against the real data folder instead).

### Where Mistery keeps its data, and one trap

Everything lives in `%APPDATA%\Mistery`: `library.db`, `settings.json`,
`mistery.log`, and the `art`, `thumbs` and `music-art` folders. Set
`MISTERY_DATA_DIR` to put all of it somewhere else — that is how the tests stay
out of your library.

The trap: a program started from inside a *packaged* Windows app (MSIX — the
Claude desktop app is one) sees a doctored view of AppData. Files that already
exist are shared, but every **new** file it writes is silently redirected into
that app's private `%LOCALAPPDATA%\Packages\…\LocalCache`. A Mistery (or a test)
run that way makes artwork only it can see while recording in the shared library
that the artwork exists — which is exactly how four album covers once turned into
placeholders. So now:

- Mistery warns and offers to close if it is started that way
  (`config.virtualized_appdata()` checks for real, by writing a probe file and
  seeing where it lands), and `selfcheck.py` says so at the top of its report.
- An album cover recorded as made but missing on disk is made again on the next
  library pass, whatever made it go missing.
- The single-instance check asks a running Mistery over its named pipe before
  opening the database. Lock files are redirected like any other new file; the
  pipe is not, so two copies can no longer run side by side on one database.

## Install to the Start Menu

```bash
powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1
```

Adds Mistery to the Start Menu with its own icon, searchable by name. Pass
`-Desktop` for a desktop shortcut too, or `-Uninstall` to remove both.

**To pin it:** press Start, type `Mistery`, right-click the result, choose
*Pin to Start*. Windows 11 has no supported API for pinning from a script —
Microsoft removed it — so that last click has to be yours.

The shortcut carries the same AppUserModelID the app sets on itself, so a pinned
shortcut and the running window share one taskbar button instead of two. Moving
the `.lnk` to the Desktop keeps that — it travels with the file.

Regenerate the icon after editing `tools/make_icon.py` with:

```bash
python tools\make_icon.py
```

## Why mpv and not a web view

The library here is 4K HEVC in MKV containers with EAC3 5.1 audio and HDR10.
A browser-based player (`<video>`, Electron, any Chromium shell) cannot play
that: MKV isn't a supported container, EAC3 multichannel isn't supported at all,
and HEVC/HDR is inconsistent. So Mistery embeds **mpv** — full GPU decoding,
every codec, real HDR tone mapping — and wraps it in a Qt interface.

mpv ships from winget as one statically linked `mpv.exe` with no `libmpv` DLL,
so instead of binding the C API, Mistery launches mpv as a child process, hands
it the Qt window handle with `--wid`, and drives it over its JSON IPC pipe.

## Requirements

| | |
|---|---|
| Python | 3.10+ (built and tested on 3.12) |
| PySide6 | `pip install PySide6` |
| mpv | `winget install shinchiro.mpv` — playback |
| ffmpeg/ffprobe | `winget install Gyan.FFmpeg` — metadata, artwork, thumbnails |
| Pillow, requests | usually already present; `pip install Pillow requests` |
| cryptography | `pip install cryptography` — movie night's per-evening certificate |

Everything is discovered on `PATH` (plus the usual install locations), and the
Settings page shows what was found.

## Features

**Interface**
- A **menu down the left**: icons at rest, their names when you rest the pointer
  on it (it opens over the page rather than pushing it aside), the page you are
  on filled yellow. Library totals and Rescan sit at its foot
- **Home** greets you for the time of day ("Good evening", and how many things
  are waiting where you left them), with a search pill; a rounded **hero** for
  what to pick up, with how far along you are and *Resume · More info · Watch
  together · Play in VR*; and a shelf for the hour ("Films for a quiet night",
  "After dark") with *See all*
- **Rest on a card on Home** and it grows and plays, from where you stopped:
  silent for two seconds, then its sound fades in — never over music, and never
  from a window that isn't in front. A title you haven't started shows its
  picture instead (no spoilers from a minute picked at random)
- Tiles **grow under the pointer** with a yellow edge and a play circle
- A deep warm black, one accent (sunflower yellow) for what is chosen, playing
  or to be pressed, titles in **Fraunces** and everything else in **Figtree**.
  Both fonts are free (SIL Open Font License) and bundled in `assets/fonts`,
  their licences beside them
- A **game controller** works all of it from the couch (below)

**Finding things in a big library**
- **Categories** on Movies and Shows: tick as many as you like — Anime, Romance,
  Action — and the page narrows to them. *Any* is the default (a film that is
  either), *All* is one click away (a film that is both). What you tick is still
  ticked tomorrow
- Four services name genres four different ways: TMDB says "Sci-Fi & Fantasy"
  for a series and "Science Fiction" for a film, TVmaze says "Science-Fiction",
  Wikidata says "science fiction film". They are mapped onto one list of names,
  so one chip means one thing. The old filter compared your choice against the
  whole genre line, which quietly put every *Sci-Fi & Fantasy* series under
  **Fantasy** and every *Action & Adventure* one under **Action**
- **Without a TMDB key, films used to have no genres at all** — 0 of 12 in the
  library this was built against. Mistery now asks Wikidata (through the
  Wikipedia article it already fetched) and then iTunes, both keyless: 11 of
  those 12 films got categories in 16 seconds, and nothing at all on the next
  pass, because both answers are cached for a month. A guess is only ever
  written where there was nothing; data from TMDB is never overwritten
- **Set your own** on a film or series page. Hand-picked categories live in
  their own column, so re-fetching titles and artwork cannot wipe them

**Playlists**
- **Playlists** in the top navigation for films and episodes, and a
  **Playlists** tab on the Music page for songs. Right-click anything —
  a song in any list, a film tile, an episode — and *Add to playlist*; the
  playlists you are already in are ticked
- **Play all** on a film playlist plays it through: when one ends, the next one
  starts, and the Up Next card names it instead of the next episode. Starting
  anything else from anywhere else drops the line-up, so a playlist cannot
  follow you around
- Reorder and remove from the same right-click menu. A playlist is a list of
  library ids, so a rescan, a renamed folder or a finished download leaves it
  alone, and anything whose file has gone is skipped rather than deleted

**Preview and handover**
- **Continue Watching tiles preview on hover**, starting at the frame you
  stopped on. No second decoder and no extra process: it animates the seek
  sprite the file already has, so it costs a JPEG that is on disk anyway.
  Only that row previews — those are the files you have started, so the sprite
  exists and the resume point is the frame worth showing
- A **dwell** before it starts, so sweeping across a row doesn't set every tile
  running
- **Pressing play grows the tile into the picture.** Starting playback has an
  unavoidable gap — mpv has to launch, open the file and decode a frame — and
  cutting to black for that long reads as broken. The artwork expands from
  where it sat and holds until video is genuinely on screen (mpv's
  `playback-restart`), which turns the gap into the transition. Whatever the
  tile was showing is what grows, including a preview frame mid-animation
- The **Up Next card carries the next episode's still**, and that still grows
  into the picture when the countdown fires — so an episode boundary is a
  handover rather than a cut through black
- Started from the hero or the keyboard instead, there is no tile to grow, so
  the title's own artwork cross-fades in place
- The cover follows the picture into fullscreen, comes off on error, and has a
  six-second timeout. Nothing about a transition is worth leaving an image
  stuck over a working player

**Video quality**
- A quality button in the player: **Data saver**, **Standard**, **Best**. The
  menu heads with the file's real resolution, because none of these add detail —
  they decide how much GPU work goes into showing what's there
- It applies **live**, mid-film. The three presets are defined once, in
  `player/mpv_process.py`, and every preset sets the same keys — that is what
  makes switching safe at runtime, since no filter can survive from the
  preset before it
- Also in Settings → Playback; the two stay in sync

**Library**
- Recursive scan of any number of folders; only video files, extras and sample
  clips skipped
- Scene-release filename parsing —
  `Night.Train.2019.2160p.BluRay.EAC3.5.1.HDR.x265-GROUP`
  becomes **Night Train** · *2019* with 4K / HDR / HEVC / EAC3 5.1 tags
- `SxxExx`, `1x04` and `Season 1 Episode 5` grouped into Show → Season → Episode
- Series names are taken from the folders when the filename has none, which is
  the common case for ripped seasons: `Harbor Lights S01/S01E01 - Pilot.mkv`,
  `Harbor Lights/Season 01/S01E01.mkv` and `Northfield/Season 1/E05 - The Signal.mkv`
  all resolve to the right show
- Parsing rules are versioned (`parser.PARSER_VERSION`). When they change, an
  existing library re-derives its titles and grouping on next launch without
  re-probing or re-thumbnailing anything, so watch progress survives
- **Downloads in progress are recognised.** A torrent that has preallocated a
  6 GB file with a zeroed header makes ffprobe report `EBML header parsing
  failed`; those are marked `incomplete` rather than `error`, kept out of the
  library listings, skipped by the artwork and thumbnail stages, and retried on
  every scan until they become readable. The foot of the menu on the left shows
  how many are pending
- **Derived data is invalidated when its source changes.** Artwork filenames and
  sprite indexes record the file size they were built from, so a file that grows
  (a download finishing) or is replaced (a better rip) never keeps stale art
- **A failure is never permanent.** Anything that errors can be requeued from
  Settings → *Retry failed*, and a poster is generated even for a film so dark
  that every sampled frame scores zero
- **Only one instance runs at a time.** Two copies both scanning and writing
  fight over the library and each reloads in response to the other's writes
- **The window notices outside edits.** `PRAGMA data_version` is polled every
  2.5 s (never during playback or while the pipeline is writing), so a
  maintenance script or a second instance updating the database refreshes the
  library in place instead of leaving a stale view until restart
- SQLite cache; a rescan of an unchanged library costs one `stat()` per file
- Tech specs read from ffprobe, not guessed from the filename

**Playback**
- Hardware-decoded HEVC/AV1/H.264 (d3d11va), HDR10 → SDR tone mapping with
  contrast recovery, or passthrough if your display is HDR
- **Resume** — every file remembers its position; Continue Watching row on Home
- **Dialogue boost** — a compressor + limiter for 5.1 tracks mastered for a
  cinema, so speech stays audible without the action peaking. Toggle with `B`
- Audio and subtitle track pickers, external `.srt`/`.ass` auto-loaded
- Chapter markers on the seek bar, chapter menu, prev/next chapter
- Seek-bar preview thumbnails from a pre-built sprite sheet

**Watching shows**
- **Intros and credits are found automatically.** There is no public database
  of intro timestamps, so Mistery does what Plex does: the intro is the stretch
  of identical audio in every episode of a season. Each episode's audio is
  fingerprinted (11 kHz spectral hashes, one 32-bit hash per ~186 ms) and
  cross-matched against its siblings; a stretch that ≥2 siblings agree on is
  the intro — per episode, so cold opens that move the intro around are handled.
  The same matching on the last minutes finds the credits music. Detection is
  deliberately conservative (≥8 s of agreement); anything it misses can still
  be marked by hand
- **Skip intro** — auto-skips (default) or shows a Skip button. Manual override
  per show: TV menu → "Intro starts/ends at this moment". Seeking back into the
  intro never force-jumps — you get the button, not a shove
- **Autoplay toggle** in the control bar (the loop icon, shown for episodes) —
  on, the next episode starts by itself when one ends; off, the Up Next card
  waits for a click instead. Same setting as Settings → *Autoplay next episode*
- **Previous / next episode** buttons sit next to the skip-back/forward pair.
  They move between episodes rather than to the start or end of the current
  file, and grey out at either end of a show. Chapters live in the chapter menu
- **Up Next** — at the credits (marked per show, or detected from a final
  chapter in the last few minutes) a card offers the next episode with an
  8-second countdown; Cancel stays cancelled for that episode. At end of file
  the card appears over the held last frame if it hasn't already
- **Subtitle and audio memory per show** — turn captions on in one episode and
  every later episode of that show starts with them on, matching language;
  same for the audio track. Per-file choices still win when you've set them
- **Next Up row** on Home — the first unwatched episode of each show you've
  started, suppressed while an episode is mid-play (Continue Watching owns it)
- Keys: `N` next episode · `P` previous episode · `I` skip intro

### Why the end of a file needs saying

mpv runs with `--keep-open=yes` so it holds the last frame instead of closing
the window. The way it holds that frame is by setting `pause=yes` — and it emits
**no `end-file` event at all**, because the file is still loaded. Two
consequences, both of which were live bugs:

- the end-of-episode path never ran, because it was listening for `end-file`.
  `eof-reached` is the only signal that playback actually ran out, so that is
  what the player watches now
- `pause` is a *global* property, so loading the next file does not clear it.
  The next episode arrived already paused and sat there until you pressed play
  twice. `MpvProcess.load()` now clears pause on every load

**Keyboard**

| Key | |
|---|---|
| `Space` / `K` | play/pause |
| `←` `→` | ∓10s (hold `Shift` for 1s) |
| `J` / `L` | ∓30s |
| `↑` `↓` | volume |
| `M` | mute |
| `F` / `F11` | fullscreen |
| `S` | subtitles on/off |
| `B` | dialogue boost |
| `N` / `P` | next/previous episode |
| `PgUp` / `PgDn` | previous/next chapter |
| `,` `.` | frame step |
| `Esc` | leave fullscreen, then leave the player |
| `/` or `Ctrl+F` | search · `Ctrl+R` rescan · `Alt+←` back |

**Game controller** — Xbox (XInput) or PlayStation (DualShock 4, DualSense).
Read only while Mistery's window is in front; Settings → *Game controller*
switches it off, or makes everything bigger for across the room.

| Xbox | PlayStation | |
|---|---|---|
| d-pad / left stick | d-pad / left stick | move the yellow glow |
| `A` | `✕` | open / press |
| `B` | `○` | back |
| `X` | `□` | Watch together |
| `Y` | `△` | Search |
| `LB` `RB` | `L1` `R1` | the row above / below |
| `Start` | `Options` | the menu on the left |
| in the player: `A` · `LB` `RB` · `LT` `RT` · `Y` · `X` · `B` | `✕` · `L1` `R1` · `L2` `R2` · `△` · `□` · `○` | pause · ∓10 s · ∓30 s · subtitles · skip intro · close |

**Mouse**

| | |
|---|---|
| move | brings the controls back, and the pointer with them |
| click the picture | play/pause |
| double-click | fullscreen (without leaving it paused) |
| scroll | volume |
| idle 2.8 s | controls fade and the pointer disappears |

The controls stay up while the pointer is resting on them, and clicking empty
space *in* the control bar doesn't pause — only the picture does.

Waking takes **16 px of travel**, measured from wherever the pointer came to
rest. An idle optical mouse drifts a pixel or two and a knock to the desk moves
it a few more; none of that should light up the screen mid-film. It's distance
travelled rather than a per-event threshold, so a slow deliberate move still
wakes it — it just has to actually go somewhere, and jitter oscillating around
one spot never accumulates.

### Why the mouse needs saying

mpv draws into its own native child window, so the controls have to live in a
separate translucent window above it. Windows hit-tests a translucent window one
pixel at a time and lets the mouse fall through wherever the alpha is zero —
straight to mpv, which is started with no input bindings and ignores it. The
effect is a player that answers the keyboard but is completely deaf to the mouse.

So the overlay paints 0.4% black across the whole picture: invisible on a video
frame, enough for Windows to treat the surface as present. It costs mpv about
1.3 points of one core while the controls are hidden, measured over three 20 s
runs each way. `smoketest.py` asks the OS which window a click at the centre of
the picture would reach, so this cannot regress unnoticed.

## Movie night

Watch a film or an episode with friends who also have Mistery, each on their
own PC, in step. It goes straight from the host's PC to theirs: no server in
between, and the website plays no part in it.

**Starting one.** On a film's page, an episode's menu or the player's menu,
choose **Start movie night**. Mistery opens one port (42170 unless you change
it), asks the router to forward it (UPnP), makes a TLS certificate for this
evening only, and shows an invite code to paste into Discord or a text. Friends
press **Movie night** at the top of their Mistery and paste it. The whole
message is fine: the code is found inside it, and a typo is caught by its
checksum before anything connects. **Copy link** gives the code as a link
instead, which chats make clickable: it opens a page on Mistery's website that
opens their Mistery's Join panel with the code filled in. The code rides after
the `#` in the link, which a browser never sends, so the website never sees it.

**Staying together.** Anyone can play, pause or seek. The host's Mistery keeps
the one clock everyone follows. Small drift is corrected by nudging playback
speed by up to 5 %, and large drift by a seek. Measured across three Misterys,
players sat 11–15 ms apart at the median and within 42–53 ms 95 % of the time.
A frame at 24 fps is 42 ms. If somebody is buffering, everybody waits for them.
A friend whose connection can't keep up with the film (three stalls in three
minutes) is offered a lighter stream, and the rest carry on without them
instead of stopping every few seconds.

**Your own place is never touched.** While a movie night runs, nothing moves
your resume point, watched state or play count for that film or episode. The
party's place is kept separately, on every PC in it, and **Movie nights** on
Home picks it back up with **Continue**.

**Quality.** The file goes as it is: a 4K film here needs 7–12 Mbit/s per
friend. A friend whose PC or connection can't take the original can choose
*Smoother (1080p)* or *Low bandwidth (720p)*. The host makes that on the fly,
on the graphics card when it has an encoder: about 0.2 of a CPU core for a
1080p episode, and 1.5 cores for 4K HDR tone-mapped down to 1080p. Audio
track, subtitles and volume stay each person's own.

**Getting friends in.**
- **At your place:** it works as soon as Windows lets Mistery use the network.
  It asks once; choose Allow. The host panel says which box to tick.
- **Elsewhere:** the port has to reach your PC. Many routers have UPnP switched
  off, and then the host panel shows exactly what to type into the router's
  page. Once it's done, tick *I've forwarded port 42170 to this PC on my router*
  in Settings → Movie night, and the panel stops repeating the steps.
- **A VPN** on the host PC usually hides your home address: pause it for the
  evening, or let Mistery bypass it (split tunnelling).
- **Carrier-grade NAT:** some providers put many homes behind one address, and
  then nothing from outside can reach you. The panel says so plainly.

When the router won't say what your internet address is, Mistery asks a public
STUN server (Cloudflare's, then Google's), and that question is all it sends.

**Security.** A movie night listens on one port, only while it runs. Everything
goes over TLS with a certificate made for that evening, and its fingerprint is
inside the invite code, so a friend's Mistery checks it is really you before it
sends a byte. The code also carries a 104-bit secret, and without it a request
gets a bare 404 or a closed connection. Only the one file being watched, and the
subtitle files named after it, can be reached: never the library, never any
other file. When the night ends, the port closes and the router's mapping is
removed. After a crash, that happens at the next start.

With library sharing on (below), the port belongs to sharing and stays open.
A movie night then runs on sharing's listener instead of opening its own, using
this PC's lasting certificate rather than one made for the evening. The code's
rules are unchanged: its guests get the one film, and when the night ends its
code stops working while friends carry on being served.

## Friends

Share your library with friends who have Mistery, and watch theirs: films,
shows and music, streamed straight from one PC to the other. Their films and
songs are never copied to your PC, there is no account, and no server sits in
between.

**Adding a friend.** Open **Friends** in the menu on the left. Either press **Get my
code** and send the code, or paste the code a friend sent under **Add theirs**.
One code is enough, sent either way. It looks like a movie night invite with a
2 in front, and pasting one in the wrong box is named rather than refused as a
typo. A code adds one friend, works for a day, and then it's spent. Sharing
switches itself on with the first friend. **Copy link** sends it as a link, as
for a movie night: a friend who clicks it gets their Friends page with the code
already in the box, and adding you is still their own press of **Add friend**.

**Their library.** **Browse** on a friend's row shows their films, shows and
albums. Their posters arrive the first time they come on screen, and are kept.
Everything they share is kept too, so you can browse it while their PC is off.
Opening it asks their PC what has changed, and only that is sent. Play works
while their PC is on: the film comes from their disk, with the subtitle files
beside it. Your place in it is kept on your PC, never in your own library's
watch history, and never on theirs.

**Watch together.** On a friend's film, or an episode's menu, **Watch together**
starts a movie night of it on their PC, even with their Mistery closed. You
join it at once, paused at the start, and the code is yours to pass on: in the
Movie night panel and the player's menu. The film goes from their PC straight to
each of you, and only their friends get in. Their PC checks each person's
certificate at the door, so a code passed further opens nothing. It ends a couple
of minutes after the last of you leaves. Its card in **Movie nights** on Home
then has **Watch together again**: their PC starts it where it got to, with a
new code to pass on. On the PC it's on, quitting Mistery while friends are
watching asks first, and the PC stays awake meanwhile.

**Their PC off, or out of reach.** The page says so, in words: off or asleep,
sharing switched off, or (from outside their home) their router not forwarding
the port. The same port as movie night, so one forward does both.

**While Mistery is closed.** With *Keep sharing while Mistery is closed* ticked
(the default), a windowless Mistery keeps serving friends from the moment you
sign in to Windows. It's listed in Task Manager's Startup apps, where you can
switch it off. It steps aside whenever Mistery itself opens, and for the few
seconds an update takes, then starts again. While a friend is actually
watching, it keeps the PC awake, never the screen; that's a box on the Friends
page too.

**Security.** Each Mistery has a certificate of its own, made once and kept.
Adding a friend swaps certificates: the code carries this PC's fingerprint and
a one-time 104-bit secret, and the friend's Mistery checks the fingerprint
before it sends anything. After that, both ends show their certificates on every
connection, and neither answers anything whose certificate isn't written down.
A friend asks for a title by kind and number. Nothing they send is ever turned
into a path, and they never see where your files are. Each thing they play gets
its own token, which is handed back when they stop. **Pause** stops serving a
friend for now. **Remove** forgets them, their library and your place in it.

## Music

Albums in your library folders appear under **Music** — Albums, Artists and
Songs — with a player bar that follows you around the whole app and a full
**Now Playing** view behind it.

| | |
|---|---|
| `Space` | play / pause — anywhere outside the film player |
| `Ctrl+←` `Ctrl+→` | previous / next song |
| media keys | play, pause, next, previous |
| `Esc` | leave Now Playing |

**What makes it Mistery's rather than a copy**

- **Synced lyrics you can click.** The sung line sits a third of the way down
  and the rest scroll past it; click any line to jump there, scroll to read
  ahead and it waits three seconds before following the song again. Sources, in
  order: a `.lrc` next to the file, lyrics in the file's own tags, then
  [LRCLIB](https://lrclib.net) — free and keyless. Only the playing song and the
  next one are looked up, every answer is cached (including "none found"), and
  it can be switched off in Settings → Music.
  Two things stop that chain from failing quietly. A lyrics tag has to *read*
  like lyrics: albums downloaded from blogs often carry the blog's address in
  that tag and nothing else — every song on one album here has the blog's web
  address in it — and a tag like that would
  otherwise beat a real synced set and never be asked about again. And when the
  exact lookup misses, the search behind it is scored on title, artist and
  running time, so a file called "Long Time (Intro)" still finds "Long Time -
  Intro" and a song credited to two artists still finds the one credited to
  one. That album went from 0 songs with lyrics to **19 of 19, all synced**
- **Every record wears its own colours.** The album page and Now Playing are
  tinted from the cover. Background colours are pulled down until white text
  clears a 7:1 contrast ratio on *any* cover, and a black-and-white cover gets a
  neutral accent rather than an invented hue
- **Shuffle that sounds random.** True randomness clusters — it will happily
  play three songs off one album in a row, which is exactly what people call
  broken. Each album's songs are spread evenly through the queue with random
  offsets instead. Over 200 shuffles of a 3-album queue the worst run was 2 in a
  row; plain random reached 6
- **Every album at the same volume.** Not from the tags — Mistery measures the
  songs itself. See [Sound](#sound) below: the library was 10 dB apart album to
  album and is now 2.2 dB
- **Downloads appear as they finish.** Nothing to rescan: while anything is
  still arriving the library checks every 20 seconds, otherwise every three
  minutes. An album shows "9 of 13 downloaded" with the rest greyed out
- **Honest quality.** *Hi-Res Lossless · 24-bit/96 kHz* comes from the stream
  itself, not the filename
- **It steps aside for film.** Starting a movie pauses the music, and doesn't
  resume it afterwards on purpose — an album kicking back in at the end of a
  two-hour film is a surprise. Discord shows *Listening to* for music and
  *Watching* for film, and film wins

### Now Playing

- **A record that turns.** The cover becomes the label of a vinyl record that
  spins at 33⅓ rpm while the song plays, spinning up and coasting down like a
  turntable, under a light sheen that stays still, which is what makes it read as
  spinning. A tonearm swings onto the record when you press play and lifts off
  when you pause, and walks inwards over the song. Only the label is redrawn
  each frame; measured at about 2.6% of one core while visible, nothing while
  paused, and nothing at all while Mistery is hidden, minimised or in the tray.
  Settings → Music switches back to the flat cover
- **Playing from** — the album, artist, search or Liked Songs the queue came
  from, at the top; click it to go there. Liked Songs played while the search
  box narrowed them says so, and opens with the same search
- **♥ Like** from Now Playing or the bar, and a **Liked** tab on the Music page
- **Sleep timer** (the moon): 15, 30, 45 or 60 minutes, the end of this song, or
  the end of the queue. The music fades out over its last 8 seconds instead of
  cutting off, your volume setting is left alone, and a film playing at the time
  is paused too, with "Paused by the sleep timer" on the picture until you press
  play. It keeps running from the tray, where the tray menu shows it
- **Next up** card, a **Details** tab (format, sample rate, measured loudness
  and the gain Mistery applies, plays, last played, date added, file location),
  and click the song length to see the time left instead
- **Before the singing starts** — Fist's first line is two minutes in, and an
  empty lyrics view looked broken. Breathing dots hold the place of the next
  line through an intro or a long instrumental gap
- **A screensaver for the lyrics tab.** Leave a song playing and don't touch
  anything for three minutes: Mistery goes full screen and shows the record
  turning, the words at the size of a poster, and the song's own shape as a
  waveform underneath, on black. F11 starts it at once. It is built to be left
  running on an OLED — the whole picture drifts 24 px over six minutes (0.4 px
  a second: invisible while you watch, 48 px of travel while you don't), it
  dims as the hours pass, nothing bright sits still, and it does **not** hold
  the display awake, because letting Windows blank the panel is the best
  protection there is
- **It ends when you actually move the mouse**, not when the mouse twitches.
  96 px of travel, measured from an anchor that resets after two seconds of
  stillness — so a night of 1 px drift never adds up to an exit — and at least
  two movements 50 ms apart, so a single jump (a remote desktop reconnecting, a
  game warping the pointer, a monitor waking) doesn't count either. A click, the
  wheel, Esc or any other key ends it immediately; play/pause, the media keys
  and the volume keys act without ending it. Switch it off, or change the three
  minutes, in Settings → Music
- **Output** in the Sound panel switches between headphones and speakers
  without stopping the song. If the headphones you chose are unplugged, the song
  carries on on the Windows default from where it was, or stays paused if it
  was paused; the choice is kept for when they are plugged back in. What
  happened is written to `mistery.log`, mpv's own account of it included

**Resume where you left off.** Close Mistery (or restart the PC) and the queue,
the song, the position, shuffle and repeat come back the next time it opens —
paused, on the song, without a sound until you press play. Your keyboard's media
keys and the Windows media overlay can start it straight away: a moment after
Mistery opens, the song is loaded, paused, so Windows knows it is there. It is
saved a moment after anything changes and every 15 seconds while playing, to
`music-session.json` in the data folder; songs deleted since are skipped, and a
song saved in its last couple of seconds starts again from the top. Switch it
off in Settings → Music.

### The volume bar

The same painted bar now serves the player bar, Now Playing and the film
player, and it behaves the way a volume control should:

- **Click where you mean it.** A click used to be a 10-unit page step, so the
  only way to reach a number was to drag. Now the bar lands exactly where you
  click — the first pixel is silence and the last one is the top of the range —
  keeps following the pointer when it leaves the widget mid-drag, and shows the
  number while you hold it
- **The wheel** works over the bar and over the film picture: 5 a notch, 1 with
  Shift. **Ctrl+↑/↓** does the same for music from the keyboard, with Shift for
  single steps, and **Ctrl+M** mutes — there was no way to change the music
  volume without the mouse before
- **Mute is a state, not a level of zero.** It is mpv's own mute, so the bar
  keeps the level it was at, remembers it across a restart, and reaching for the
  volume unmutes rather than changing a number nobody can hear — Ctrl+↓ out of
  silence comes back one step quieter, not at whatever it was before
- **The bottom of the bar is no longer dead.** mpv's volume is cubic — 20 was
  −42 dB, so the first tenth of the travel was silence with extra steps. The bar
  maps travel to about 0.4 dB per percent, which spreads roughly 40 dB across
  its length. The number stored is still mpv's own, because the sleep timer's
  fade multiplies it; only the mapping from pixels changed. If you had it at
  70, the handle now sits further right for exactly the same loudness
- **It stopped writing to disk on every pixel.** Dragging used to save
  settings.json once per step — the file whose own notes blame "every volume
  tick" for a past corruption. mpv hears every step immediately; the file is
  written 400 ms after you stop

### Sound

Two complaints started this: the music was too quiet, and one album played far
louder than the others. Both had the same cause. The rock albums' FLACs carry
ReplayGain tags asking to be played 11–13 dB down; the loud album's MP3s carry none.
mpv was doing exactly as it was told — so the tagged albums played at −18 LUFS,
the untagged one at −8, and all of them sat below what any streaming service
gives you.

Tags were never going to fix that, because half the library doesn't have them.
So Mistery measures the music itself: **EBU R128 integrated loudness and true
peak, once per file** — about half a second of decoding per song, in the
background alongside the artwork pass — and then hands mpv one number per file.

| album | as mastered | played before | played now |
|---|---|---|---|
| hip-hop album, MP3, untagged | −8.1 LUFS | −8.1 | −11.0 |
| rock album A, FLAC, tagged | −7.1 | −17.1 | −11.0 |
| rock album B, FLAC, tagged | −6.3 | −18.0 | −11.0 |
| a folder of singles | −4.7 | −18.1 | −11.0 |
| rock album C, FLAC, tagged | −11.7 | −18.0 | −13.2 |
| **loudest to quietest** | | **10.0 dB** | **2.2 dB** |

Rock album C is the whole design in one row. It is a quiet master whose peaks
already touch full scale, so it cannot be lifted to −11 without clipping, and
its measured true peak stops the gain exactly where the clipping would start.
Nothing is ever pushed past −1 dBFS: a song that still ends up quiet is one its
own peaks held there.

**Match volume** picks the target — Off, Quiet (−18), Normal (−14) or **Loud
(−11)**, the default, which is what streaming services use for their loudest
setting. An album played in order moves as one record, so its quiet songs stay
quiet; a shuffle is levelled song by song, since it is a mix of records.
**Extra loudness** (+3 dB by default, or +6) goes on top with a lookahead
limiter, because these masters have no headroom left to give: the +3 setting
measured +2.7 dB of real loudness with the limiter touching only the hottest
album, by 0.9 dB.

The other three are tone and space, and each was measured so that switching it
on changes the sound and *not* the loudness — otherwise every comparison is
just "louder sounds better":

| | what it does | measured |
|---|---|---|
| **Bass** — Warm / Deep / Massive | a low shelf at 95 Hz | +2.7 to +5.9 dB in the 20–120 Hz band at Deep, depending on how much was down there already |
| **Clarity** | a 3 dB lift from 8 kHz up | +0.9 dB above 4 kHz |
| **3D audio** — Subtle / Normal / Wide | widens the stereo picture, then puts one channel 5–14 ms behind the other | side-to-middle energy rises from −9.1 dB to −0.2 dB at Normal, while the loudness moves −0.2 LU |

**The 3D effect had to be rebuilt once.** The first version used ffmpeg's Haas
enhancer, which is the textbook answer: it makes each output channel from a
delayed copy of the middle of the mix plus the sides. It was wrong in a way you
can hear — things went *missing* from the music — and the reason shows up in a
measurement. Rebuilding a channel out of delayed copies of itself cancels
frequencies inside that one channel, so one ear's spectrum came out rippling by
3.3–5.0 dB with dips as deep as **−24 dB**. Whole bands of a voice or a cymbal,
gone.

What replaced it is what [audioalter's 3D tool](https://audioalter.com/3d-audio)
actually does, read off its own before/after examples: the stereo picture is
widened (its side level came out about 2.4×), and then one whole channel is put
**8.5 ms** behind the other. Nothing is subtracted anywhere, so each ear still
gets the entire mix — one of them slightly later. Same measurement on the same
songs: 0.4–1.7 dB of ripple, and all of it is the widening changing the balance
rather than notches; a plain channel delay on its own measures 0.00 dB. Mistery
uses 2.4× and 9 ms for **Normal**, with Subtle and Wide either side.

It is still a headphone effect, and now for a different reason: the two arrivals
never meet in the air, because each ear gets its own. On speakers they do meet,
and it will sound hollow.

A bass shelf can't be free in the same way — these records are mastered against
the wall — so half of each boost is taken back as headroom and half is left to
the limiter, which measured as the cleanest split. Widening is levelled the same
way, by a constant measured over six mixes, so switching 3D on costs about
−0.2 LU on a normal mix (a very narrow one loses more, up to 1.8).

Everything lives under the **Sound** button next to the volume, and in
Settings → Music. Changes are heard immediately, mid-song.

**What it costs.** Decoding a 318-second 24-bit/96 kHz FLAC as fast as the
machine will go takes 0.46 s of CPU. With the volume gain, the 3D enhancer and
both shelves, it still takes 0.46 s — these filters are free at this scale. The
limiter is the one stage that costs anything: 59 ms of CPU per minute of audio,
about a tenth of a percent of a core. Which is why it is only added when a
song's measured true peak says that song, with these settings, can actually
reach full scale — and why, with everything off, there is no filter graph at
all and mpv does no extra work.

**How the gain rides with the file.** mpv applies all of it as one libavfilter
graph, and every entry in its playlist carries its own: gain first, effects
next, limiter last, so nothing can raise the level after the limiter has
protected it. That ordering matters at the boundary between two songs — a gain
applied when the next song *starts* leaves its first moments playing at the
previous song's volume, which is audible on a gapless album. Handing mpv the
graph as a per-file option means it is already in place. (The option list is
comma-separated and a filter graph is full of commas, so the value goes over
mpv's IPC in its length-escaped `%<bytes>%<value>` form; a plain string is
accepted and then silently ignored.) Album boundaries were then checked in mpv's
own log: the audio output is not reopened, so playback stays gapless.

### Playing with no window

Close the window while music is playing and Mistery goes to the system tray and
keeps playing. With nothing playing, closing quits as it always did. There is a
switch for it in Settings → Music.

In the tray it stops everything that isn't the music:

| | while hidden |
|---|---|
| library scanning, artwork, seek thumbnails | paused — these run ffmpeg |
| download watcher, database watcher | stopped |
| lyric lookups | skipped until the window comes back |
| mpv's position stream (~15 updates/s) | off; the position is asked for every 5 s |
| mpv's pipe reader | dozes up to 100 ms instead of 4 ms |

Measured while playing the same album: **1.9% of one core for a merely hidden
window, 0.2–0.4% in the tray**. mpv's own ~1% is the actual FLAC decode and
resampling, which is the price of the music. Hiding a window on its own saved
almost nothing (1.98% → 1.87%) — Qt stops painting and that was never the cost.

- **Media keys keep working inside a game.** mpv registers a Windows media
  session (`--media-controls`), so play/pause/next on a keyboard reach Mistery
  with a game in the foreground, and Windows' own media overlay shows the song.
  Verified by driving that session through the Windows API rather than by
  pressing keys, which would also have hit any other player
- **Right-click the tray icon** for the song, play/pause, next and previous
- **Opening Mistery again brings the window back** instead of starting a second
  copy or saying "already running" about a window that isn't there
- **A crash can't leave music playing.** mpv is put in a Windows job object tied
  to Mistery's lifetime, so ending Mistery — even with End Task — ends mpv.
  Otherwise a crash in the tray leaves an album playing with nothing to stop it
- **Minimising** switches off the position stream too, but leaves the library
  working: minimising isn't a request to stop everything

### Telling a finished download from a partial one

Albums arrive by torrent, and a torrent file on disk lies in three ways — all
three turned up in the first two albums added:

| on disk | what a tag reader sees | caught by |
|---|---|---|
| preallocated, header still zeroed | nothing | the header won't parse |
| header arrived, audio hasn't | a 0.1 MB file claiming 3:45 | size vs. duration |
| right size, pieces missing inside | a normal-looking file | sampling for zeroed holes |

A fourth needs more than that: everything present except the last piece. For
FLAC, the final frame in the file is decoded and its sample position compared
with the total in STREAMINFO — the audio has to actually reach the end the
header promises. Every "incomplete" verdict on the real downloads was backed by
evidence (Rapture: 29 of 83 MB entirely zero), and none of the finished files
tripped it.

Tracks that fail are never queued or played, and are re-read on every pass
*regardless of timestamps*: a torrent can write its last piece within the same
second a scan saw it, and a finished file never changes again — trusting mtime
there would leave a completed song stuck as "downloading" for good.

### Why these choices

- **mutagen, not ffprobe, for tags.** 2.1 ms per file against 73 ms, measured
  on the real albums — ffprobe's time is mostly starting a process. It also
  hands over embedded cover art as bytes, with no extra ffmpeg call. ffprobe is
  still the fallback if mutagen is missing
- **A second, windowless mpv.** Music keeps playing while you browse, and a
  film can pause it without tearing it down. `--vid=no --audio-display=no`,
  because to mpv an embedded cover is a video stream and would open a window
- **The queue is mirrored into mpv's playlist** for gapless playback: mpv can
  only join two songs without a gap if it already has the next one open. The
  current song is identified from mpv's `path`, never from playlist positions —
  rebuilding the playlist and position events cross in the pipe, and a late
  position would pick the wrong song
- **Play state is judged from both `pause` and `idle-active`,** re-read on
  either event. mpv sends them in no fixed order, and judging on one showed ▶
  over a song that was plainly playing. A test now forces every ordering
- **The current song is announced by identity, not by queue position.** Playing
  something new at the same position as the old queue — Play on one album then
  Play on another, both position 0, or anything at all with shuffle on — left
  the bar, the lyrics, the highlighted row and Discord showing the previous
  song. The playing song is likewise accepted only when mpv's `path` and
  `playlist-pos` agree with each other, so a late event from before a queue
  swap can't select the wrong song
- **Loudness is measured, not read.** ReplayGain tags would have been free, but
  they only exist on half these albums, and the ones that have them ask for a
  reference level from the 1990s. One pass with one yardstick is the only way
  every album can play at the same volume — see [Sound](#sound). A tag is still
  used as a stand-in for a song that hasn't been measured yet, shifted to the
  same target
- **Commands to mpv go out in a byte window.** mpv answers every command with a
  reply, and its IPC thread writes each reply (and event) before reading the next
  command — waiting, if Mistery is not reading. The pipe holds about 4 KB that mpv
  has not read. The pump used to write everything queued before reading anything,
  so a big enough burst ended with both sides waiting on each other for good. mpv
  played on, which is what made it look like a display bug: Shuffle on a 92-song
  artist left the bar frozen on the first song, showing it paused, with every
  button and close-to-tray silently ignored. The limit had been about 150 songs
  until each command started carrying its own sound settings.
  Now the bytes of commands mpv has not answered stay under 4,000, and replies
  are read between every write, so a write that fits can never block. The first
  fix counted *commands* (eight at a time); reviewers wedged it anyway with
  600-byte commands during a burst of mpv output, which a long non-English path
  plus effects could reach. Verified against their reproduction: the command
  window wedged, the byte window passed at 604 and 2,004 bytes and with mpv
  stalled mid-burst. It also backs off instead of polling while mpv is stuck,
  sends paths as raw UTF-8 (half the bytes for Cyrillic or Japanese), and
  releases anyone waiting on a reply the moment mpv exits rather than at their
  timeout
- **Loudness is measured, not read.** ReplayGain tags would have been free, but
  they only exist on half these albums, and the ones that have them ask for a
  reference level from the 1990s. One pass with one yardstick is the only way
  every album can play at the same volume — see [Sound](#sound). A tag is still
  used as a stand-in for a song that hasn't been measured yet, shifted to the
  same target
- **Commands to mpv go out in a window of eight.** mpv answers every command
  with a reply, and its IPC thread writes that reply before reading the next
  command. The pump used to write everything queued before reading anything, so
  a big enough burst filled the pipe with unread replies: mpv stopped reading to
  wait for Mistery, Mistery's write waited for mpv, and neither ever moved again.
  mpv played on, which is what made it look like a display bug — Shuffle on a
  92-song artist left the bar frozen on the first song, showing it paused, with
  every button and close-to-tray silently ignored. The threshold was about 150
  songs until each command started carrying its own sound settings, which
  brought it down to 92. Now no more than eight commands go unanswered, and
  whatever mpv has said is read between every write: 276 songs queue in 0.11 s,
  and the user's exact steps went from 0/16 to 18/18
- **A song still downloading is never measured,** and a measurement is thrown
  away whenever the file's bytes change: a half-written torrent would be
  measured as whatever had arrived so far

## Discord Rich Presence

Settings → *Discord Rich Presence* shows what you're watching or listening to
on your profile: *Watching Mistery* with the film and Discord's own countdown to
the end, or *Listening to Mistery* with the song, the artist and the album. A
film playing wins over music.

Discord requires every app to have its own application registered, so this needs
a one-minute setup: [discord.com/developers/applications](https://discord.com/developers/applications)
→ **New Application** → copy the **Application ID** into Settings → *Save and
test*. Name the application `Mistery`, since Discord uses that as the card's
heading. For the fallback icon, upload an image named `mistery` under Rich
Presence → Art Assets. There is a *Hide titles* option if you'd rather it just
say something is playing.

### The picture on the card

**Most films and shows need nothing.** Their artwork came from TMDB, TVmaze or
Wikipedia, which are public, and Mistery keeps each picture's web address beside
the downloaded file. The card hands Discord that address and Discord fetches the
poster itself: no upload, no token, nothing of your account involved. Discord
documents this for activity images ("specify the URL as the field's value"), and
its own example is a music card with an album-cover URL.

Two kinds of picture have no web address, and those still go through Discord's
Art Assets by hand:

- **Album covers**, which come out of the music files themselves.
- **Films and shows nothing online matched**, whose poster Mistery cut from the
  film's own frames.

**Settings → Export posters for Discord** writes exactly those, and only the ones
Discord doesn't already have — it reads the application's list first (a public
endpoint, no token). Drop the folder into Rich Presence → Art Assets.

- Cards are **1024x576**, the 16:9 shape Discord requires. A 2:3 poster or a
  square cover goes whole in the centre over a blurred, darkened copy of the
  backdrop (or of itself). Cropping would cut away the faces and the title
- A film's name is its title (`Harbor Lights` → `harbor-lights`); an album's is
  `alb-`, the artist and the title, so two *Greatest Hits* by two artists stay
  apart, and so does an album named like a film. Names over Discord's 32
  characters end in a short hash, so two long titles can't collide
- Discord allows **300** images per application. When the export would pass
  that, what gets played most goes first and the note says what was left out
- An image name Discord doesn't have shows **no picture at all**, so Mistery
  only asks for one once Discord's list confirms it, and reads that list again
  every ten minutes — an upload shows up without restarting. Anything else gets
  the `mistery` icon
- An address is sent only if it is public: https, a host the internet can
  resolve, no login in it, no query string (the one place a key or a signed
  link would hide), at most 512 characters. A local path never leaves the PC

Uploading to Art Assets can't be automated within Discord's rules: there is no
upload API, and the one the developer portal uses needs your personal account
token, which Discord treats as a self-bot. So "automatic" here means "not
needed", not "done for you".

The connection lives on a worker thread and every failure is swallowed — Discord
being closed, uninstalled, or rejecting the ID can never disturb playback.
Presence clears when you stop watching and when the app exits.

### Artwork that briefly won't read

A single failed `stat()` is not proof a file is gone — antivirus scans, backup
software and search indexers all lock files momentarily, and treating that as
permanent leaves a placeholder on screen forever. Observed live: 70 of 109
artwork files reported unreadable on one launch and 0 two minutes later, with
the files themselves untouched.

So each load retries three times over ~1.5 s, the log records the `errno`
(2 = genuinely absent, 13/32 = held by another program), and anything still
failing gets one more attempt nine seconds after startup.

## Things that used to go wrong

A sweep of the whole app went looking for failures a real session hits:
independent finders per subsystem, each required to reproduce what it
reported, then fixes, each with a test that fails on the old code and passes
on the new, then a separate reviewer per area trying to break the fixes. The
user-visible ones:

**Watching**
- Binge-watching skipped every other episode: the new episode received the
  previous file's last position, reached "the credits" at once and showed its
  own Up Next card. Positions are ignored until the new file has opened
- Leaving the player during the Up Next countdown (Esc, Back, close to tray)
  started the next episode anyway, hidden, with the invisible controls window
  catching clicks. Closing the player now ends the whole session
- *Mark watched* / *unwatched* on the last title was undone by the next film or
  by quitting; moving on through Up Next before 92% left an episode unwatched
- Resuming turned subtitles on even when they had been off, and the per-show
  audio and subtitle memory was never applied. Tracks are now chosen by mpv
  while it opens the file, from what you last had on
- Pausing now holds the Up Next countdown; a half-watched next episode resumes
  where you left it instead of from 0:00; N on the last episode no longer
  closes the player; Settings for hardware decoding, HDR and quality reach the
  running player
- Alt+Left or Ctrl+F during a film left it playing with no way back to it

**Music**
- Space after clicking Play restarted the album (it re-clicked the button)
- Repeat all looped ~60 times a second when nothing could play (files moved, no
  audio device), and its first pick of a session played track 1 instead
- The Windows media Stop key is a plain stop; skipping to another song past the
  first one's halfway mark no longer counts the new one as played; Repeat one
  counts each loop, but not a seek made from the Windows media overlay
- The bar's volume and the Now Playing volume stay in step

**Library**
- Renaming a show or album folder kept its watch history and play counts
  (the rows follow the files instead of being replaced)
- A library drive briefly unavailable no longer deletes its albums
- A nearly finished torrent download was marked ready and played with chunks
  missing; a preallocated video still downloading counted as complete
- Anime release names: `- 05v2` revisions, four-digit episodes (One Piece),
  `Show.E05` inside a season folder, and episodes numbered like resolutions
  (`One Piece - 720`) are episodes; `Borat - 1492` and `Film - 2160 [4K]` stay
  films
- One offline metadata pass no longer leaves shows without titles and posters
  for good, and a refetch that can't reach the internet no longer replaces real
  posters with frame grabs
- Loudness measuring and artwork ffmpeg stop while a film plays or Mistery sits
  in the tray, and run at low priority

**The rest**
- Mistery could freeze for good if Discord accepted the connection but never
  answered it; Discord presence clears when a film closes and returns after
  Discord restarts
- A damaged library opens a repair offer instead of no window at all, and
  `tools/repair_db.py` no longer drops music data while reporting success
- An unreadable `settings.json` is kept aside rather than silently replaced
- The app's own background writes stopped reloading every page every 20 s while
  downloads ran (which wiped half-typed Settings fields and song selections)
- *Recently added* sorts by date, not reverse alphabetically; Back returns to the
  right album or artist; the show page keeps the season you were on
- Lyrics lookups run on their own thread and give up after a few seconds when
  the lyrics service is down, instead of holding up downloads and quitting

Left for later: the Windows media *Previous* key on the first queued song (mpv
handles it internally), specials (`S01SP1`, OVA) as season 0, and recognising a
folder renamed back to the exact name of older missing rows.

## Logs

Mistery runs under `pythonw.exe`, which has **no console**: `sys.stdout` and
`sys.stderr` are `None`, so tracebacks and Qt warnings would be discarded
silently. Everything is written to `%APPDATA%\Mistery\mistery.log` instead —
unhandled exceptions, errors raised inside Qt callbacks (which bypass the normal
hook), Qt's own warnings, and an artwork audit at startup. Rotated at 512 KB.

## VR

**Play in VR** on a film's page hands the file to a VR video player. Two kinds
of target are detected, and they work differently:

- **Dedicated VR players** — DeoVR, Skybox VR, Whirligig, Simple VR Video
  Player, Moon VR, HereSphere, Bigscreen. Found automatically in any Steam
  library (read from `libraryfolders.vdf`) or Program Files. Mistery launches
  them with the file path.
- **Desktop streaming** — Meta Horizon / Quest Link, Virtual Desktop, SteamVR.
  These do not open files; they put your monitor inside the headset. For these,
  Mistery offers to play the film fullscreen itself.

The active OpenXR runtime is also reported, which is the clearest signal that a
headset stack is actually set up.

Targets are ranked so the automatic choice is never surprising: a player whose
command line is known beats desktop streaming, which beats a player whose
command line is only guessed at (SteamVR Media Player ships with SteamVR and
does open video, but its arguments are undocumented, so it is offered rather
than chosen). Mirror and dedicated-player installs are located by exact path —
fuzzy-matching executables inside an install picks the wrong one.

Settings → *VR / headset* lists what was detected, lets you force a specific
target, and takes a path to any other player with a `{file}` argument template.
If nothing is installed, the button explains the options rather than failing
silently.

Mistery does **not** render to a headset itself. That would need the decoded
frames as a GPU texture — meaning `libmpv`'s render API plus an OpenXR loop,
rather than the child-process embedding used here.

## Artwork and descriptions

Source chain, best available wins:

1. **TMDB** — when a free API key is set (Settings → Artwork and metadata).
   Posters, backdrops, plots, genres, ratings for everything.
2. **Keyless online** — no key needed, on by default:
   **TVmaze** for shows and episodes (official episode titles, plot summaries,
   episode stills, show poster, genres, rating) and **Wikipedia** for movies
   (poster and plot from the film's article). Wikipedia is searched rather than
   guessed at by article name: filenames can't contain `:`, so
   `Night Train - First Light` has to find
   *Night Train: First Light*. Both APIs are throttled to one
   request per host per 0.8 s and back off on HTTP 429.
3. **The file itself** — several frames are sampled, scored for brightness,
   detail and colour, and the best becomes the poster; HDR sources are
   tone-mapped first. Movies always get their backdrop this way, since the
   online sources only carry posters.

Everything is cached on disk, so a second pass costs no network at all.

**Better data is never downgraded.** A row sourced from TMDB, TVmaze or
Wikipedia keeps its title and summary through re-parsing, rescans and file
changes — only its artwork can be topped up from the file. Art cut from the
video is recorded as `fallback`, not `done`, so an outage or a rate-limit is
retried on the next pass instead of becoming permanent. Settings →
*Re-fetch titles and artwork* forces the whole library through the chain again.

## Layout

```
main.py                  entry point
assets/icon.ico          app icon (generated)
tools/                   make_icon.py · install_shortcut.ps1
app/
  config.py              settings + tool discovery      db.py       SQLite cache
  parser.py              scene-release filenames        probe.py    ffprobe
  scanner.py             folder walk + reconciliation   models.py   view models
  images.py              async image loading            workers.py  background pipeline
  metadata/  tmdb.py · artwork.py · thumbs.py
  player/    mpv_process.py · audio_filters.py
  ui/        theme.py · main_window.py · home_view.py · library_view.py
             detail_view.py · show_view.py · settings_view.py
             player_view.py · player_overlay.py · widgets/
```

State lives in `%APPDATA%\Mistery` — `library.db`, `settings.json`, `art/`,
`thumbs/`. Deleting that folder resets everything; your video files are never
touched, and Mistery never writes to your library folders.

## Notes

- The transport controls live in a separate translucent window layered over the
  video. mpv renders into a native child window, and native children always
  paint above Qt widgets in the same top-level, so overlaying them any other way
  doesn't work.
- Thumbnail generation makes **one** ffmpeg pass per file, decoding only
  keyframes with hardware acceleration. Measured on a 2h21m 4K HEVC file:

  | strategy | CPU per film |
  |---|---|
  | one fast-seek ffmpeg per thumbnail | 456 s |
  | one pass, keyframes only, software | 23 s |
  | one pass + fps filter, d3d11va | **13 s** |

  The per-seek approach loses because each of ~120 processes re-opens and
  indexes a 6 GB MKV. Tone mapping turned out to be only 5% of the cost — it is
  the decoding that matters, so the fix was decoding less, not moving filters.
- All background ffmpeg runs at below-normal priority, and the whole pipeline
  pauses while something is playing (both adjustable in Settings).
- `Video quality` in Settings controls mpv's scaler cost. It used to be pinned
  to mpv's heaviest preset; the default is now `balanced`.
- mpv options are validated against the installed binary at launch and unknown
  ones are dropped, so an mpv upgrade that renames a flag won't stop playback.
- Empty-state body text uses `WrapLabel`, which measures itself with a
  `QTextDocument`. Plain `QLabel.heightForWidth` under-reports wrapped rich text
  (100px of text measured as 64px here) and silently clips the last paragraph.
