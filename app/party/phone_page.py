"""The page a phone opens to watch a movie night (phone.py serves it).

One file, nothing fetched from anywhere else: the host's PC may have no
internet the phone can reach, and the page must work on the Wi-Fi alone.

What it does, in the order a person meets it:
  - asks for a name (kept on the phone) and a tap on Join. The tap matters: iOS
    plays sound only after the person touches the page, so Join starts the
    video and pauses it again, and from then on the page may play it itself.
  - takes a seat in the room (POST join), then keeps asking what the room is
    doing (GET sync, which waits up to 2 s for news), and says where its video
    is and whether it is loading. Each answer carries the host's clock, so the
    page works out where the room is right now, as a PC's Mistery does.
  - keeps the video where the room is, with sync.DriftController's rules:
    within 80 ms leave it, to 1.5 s play 5 % faster or slower, beyond that
    seek, aiming where the room will be when the seek lands (learnt from each).
    A held room is a held frame; a scheduled start starts on the room's clock.
  - turns the phone's own play, pause and seek (Safari's controls) into the
    room's, and never its own: the video moves when the room says so, for
    everyone at once. A pause iOS makes by itself (the screen locking, leaving
    full screen) is not a pause for everyone.
"""

from __future__ import annotations

import json

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0B0A09">
<meta name="referrer" content="no-referrer">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Movie night</title>
<style nonce="__NONCE__">
:root { --bg: #0B0A09; --panel: #171513; --line: #2B2723; --text: #F4EFE7; --dim: #ABA398;
        --faint: #756E65; --accent: #FFD23F; --ink: #1B1500; --warn: #FFB35C; }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       -webkit-text-size-adjust: 100%; min-height: 100vh; }
main { max-width: 760px; margin: 0 auto;
       padding: max(18px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
                max(28px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
.eyebrow { color: var(--accent); font-size: 12px; font-weight: 700; letter-spacing: .14em; margin: 4px 0 10px; }
h1 { font-size: 26px; line-height: 1.2; margin: 0 0 6px; overflow-wrap: anywhere; }
h2 { font-size: 17px; line-height: 1.3; margin: 14px 0 2px; overflow-wrap: anywhere; }
p { margin: 0 0 12px; }
.dim { color: var(--dim); }
.faint { color: var(--faint); font-size: 14px; }
label { display: block; color: var(--dim); font-size: 14px; margin: 22px 0 6px; }
input[type=text] { width: 100%; font: inherit; font-size: 17px; color: var(--text); background: var(--panel);
       border: 1px solid var(--line); border-radius: 12px; padding: 13px 14px; outline: none; }
input[type=text]:focus { border-color: var(--accent); }
button { font: inherit; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.primary { display: block; width: 100%; margin: 16px 0 14px; padding: 15px 20px; border: 0; border-radius: 999px;
       background: var(--accent); color: var(--ink); font-size: 17px; font-weight: 700; }
.primary:disabled { opacity: .55; }
.ghost { padding: 9px 16px; border: 1px solid var(--line); border-radius: 999px; background: transparent;
       color: var(--text); font-size: 15px; }
.err { color: var(--warn); min-height: 1.4em; }
.frame { position: relative; margin: 0 calc(-1 * max(16px, env(safe-area-inset-left))); background: #000; }
@media (min-width: 600px) { .frame { margin: 0; border-radius: 14px; overflow: hidden; } }
video { display: block; width: 100%; aspect-ratio: 16 / 9; background: #000; }
.cover { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
       background: rgba(0,0,0,.45); }
.cover[hidden] { display: none; }
.cover button { padding: 14px 24px; border: 0; border-radius: 999px; background: var(--accent); color: var(--ink);
       font-size: 17px; font-weight: 700; }
.status { display: flex; align-items: center; gap: 8px; margin: 4px 0 0; color: var(--dim); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); flex: none; }
.dot.on { background: #6BD68A; }
.dot.wait { background: var(--warn); }
.news { color: var(--text); min-height: 1.45em; margin: 6px 0 0; transition: opacity .4s; }
.news.gone { opacity: 0; }
.people { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0; padding: 0; list-style: none; }
.people li { background: var(--panel); border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px;
       font-size: 14px; }
.people li span { color: var(--faint); }
.row { display: flex; align-items: center; gap: 10px; margin: 18px 0 0; flex-wrap: wrap; }
.row .grow { flex: 1; }
select { font: inherit; font-size: 15px; color: var(--text); background: var(--panel); border: 1px solid var(--line);
       border-radius: 10px; padding: 8px 10px; max-width: 60vw; }
/* A phone held sideways: the picture at the screen's full height, the rest below. */
@media (orientation: landscape) and (max-height: 540px) {
  main { padding-top: 0; }
  .eyebrow { display: none; }
  .frame { margin: 0 calc(-1 * max(16px, env(safe-area-inset-right))) 0
                  calc(-1 * max(16px, env(safe-area-inset-left))); border-radius: 0; }
  video { height: 100vh; height: 100dvh; aspect-ratio: auto; object-fit: contain; }
}
[hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <div class="eyebrow">MISTERY · MOVIE NIGHT</div>

  <section id="join">
    <h1 id="title"></h1>
    <p class="dim" id="with"></p>
    <label for="name">Your name</label>
    <input type="text" id="name" maxlength="40" autocomplete="nickname" autocapitalize="words"
           spellcheck="false" placeholder="Phone">
    <button class="primary" id="go">Join</button>
    <p class="err" id="joinerr"></p>
    <p class="faint">It plays here in step with everyone. Pause, play and skip work for everyone, as they do
      on a PC. Keep this phone on the same Wi-Fi as the PC.</p>
  </section>

  <section id="watch" hidden>
    <div class="frame">
      <video id="v" playsinline webkit-playsinline controls preload="auto"></video>
      <div class="cover" id="tap" hidden><button id="tapbutton">Tap to play</button></div>
    </div>
    <h2 id="now"></h2>
    <div class="status"><span class="dot" id="dot"></span><span id="status">Joining…</span></div>
    <div class="news gone" id="news"></div>
    <ul class="people" id="people"></ul>
    <div class="row">
      <select id="subs" hidden aria-label="Subtitles"></select>
      <span class="grow"></span>
      <button class="ghost" id="leave">Leave</button>
    </div>
  </section>

  <section id="over" hidden>
    <h1>Movie night</h1>
    <p id="reason"></p>
    <button class="primary" id="again">Join again</button>
  </section>
</main>
<script type="application/json" id="boot">__BOOT__</script>
<script nonce="__NONCE__">
(function () {
"use strict";
var BASE = location.pathname.replace(/[^\/]*$/, "");
var boot = JSON.parse(document.getElementById("boot").textContent);
function $(id) { return document.getElementById(id); }
var v = $("v");

// --- who this phone is ------------------------------------------------------------
function load(key) { try { return localStorage.getItem("mistery." + key); } catch (e) { return null; } }
function save(key, value) { try { localStorage.setItem("mistery." + key, value); } catch (e) {} }
function makeId() {
  var bytes = new Uint8Array(16), out = "";
  crypto.getRandomValues(bytes);
  for (var i = 0; i < bytes.length; i++) out += ("0" + bytes[i].toString(16)).slice(-2);
  return out;
}
var me = load("id");
if (!/^[0-9a-f]{32}$/.test(me || "")) { me = makeId(); save("id", me); }
var myName = load("name") || "";

// --- the host's clock (sync.ClockSync's arithmetic) ----------------------------------
var samples = [], offset = null;
function local() { return performance.now() / 1000; }
function hostNow() { return local() + (offset || 0); }
function sample(t0, t1, t2, t3) {
  var rtt = (t3 - t0) - (t2 - t1);
  if (!(t3 >= t0) || !(t2 >= t1) || rtt < -0.002 || rtt > 10) return;
  samples.push([Math.max(0, rtt), ((t1 - t0) + (t2 - t3)) / 2]);
  if (samples.length > 16) samples.shift();
  var quick = samples.slice().sort(function (a, b) { return a[0] - b[0]; }).slice(0, 4)
    .map(function (s) { return s[1]; }).sort(function (a, b) { return a - b; });
  var m = quick.length >> 1;
  offset = quick.length % 2 ? quick[m] : (quick[m - 1] + quick[m]) / 2;
}

function call(method, path, body, timeout) {
  var t0 = local(), options = { method: method, cache: "no-store", headers: {} };
  var abort = window.AbortController ? new AbortController() : null;
  if (abort) { options.signal = abort.signal; setTimeout(function () { abort.abort(); }, (timeout || 8) * 1000); }
  if (body !== undefined) { options.body = JSON.stringify(body); options.headers["Content-Type"] = "application/json"; }
  return fetch(BASE + path, options).then(function (response) {
    var t3 = local();
    if (!response.ok) { var e = new Error("HTTP " + response.status); e.status = response.status; throw e; }
    return response.json().then(function (data) {
      if (typeof data.t1 === "number" && typeof data.t2 === "number") sample(t0, data.t1, data.t2, t3);
      return data;
    });
  });
}

// --- the room ------------------------------------------------------------------------
var st = null, people = [], version = 0, newsSeen = 0, joined = false, over = false, joining = false;
var videoUrl = null, loading = false, quick = 0, polling = false, lastHeard = local();

function positionAt(h) {
  var p = st.position;
  if (st.playing && h > st.at) p += (h - st.at) * (st.rate || 1);
  var d = st.media && st.media.duration;
  if (d) p = Math.min(p, d);
  return Math.max(0, p);
}
function moving(h) { return st.playing && h >= st.at; }

function nameOf(id) {
  if (id === me) return "you";
  for (var i = 0; i < people.length; i++) if (people[i].id === id) return people[i].name;
  return "a friend";
}
function andList(names) {
  if (names.length < 2) return names.join("");
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

function apply(data) {
  lastHeard = local();
  if (data.gone) { if (joined && !over) rejoin(); return; }
  if (typeof data.v === "number") version = data.v;
  if (data.people) { people = data.people; showPeople(); }
  if (data.state && (!st || data.state.seq !== st.seq)) {
    st = data.state;
    hold = null;
    follow();
  }
  (data.news || []).forEach(function (item) {
    if (item[0] > newsSeen) { newsSeen = item[0]; say(item[1]); }
  });
  if (data.title) { $("now").textContent = data.title; document.title = data.title + " · Movie night"; }
  if (data.video && data.video !== videoUrl) setVideo(data.video);
  if (data.subs) subtitleFiles = data.subs;
  if (data.ended !== null && data.ended !== undefined) end(data.ended || "The movie night has ended.", data.final);
  render();
}

function report() {
  var q = "&buf=" + (buffering ? 1 : 0);
  if (st && offset !== null && !buffering && v.readyState >= 2 && !v.seeking)
    q += "&pos=" + v.currentTime.toFixed(3) + "&at=" + hostNow().toFixed(4) + "&seq=" + st.seq;
  if (notes.length) { q += "&ev=" + encodeURIComponent(notes.join("; ").slice(0, 400)); notes = []; }
  return q;
}
function syncPath(wait) {
  return "sync?id=" + me + "&v=" + version + "&n=" + newsSeen + "&wait=" + wait + report();
}
function poll() {
  if (!joined || over || polling) return;
  polling = true;
  var wait = quick > 0 ? 0 : 2;
  call("GET", syncPath(wait), undefined, 8).then(function (data) {
    polling = false;
    if (quick > 0) quick--;
    apply(data);
    setTimeout(poll, quick > 0 ? 100 : 0);
  }, function (e) {
    polling = false;
    if (e && e.status === 404) {
      end("This movie night is over. To watch the next one, scan its code on the PC.", true);
      return;
    }
    if (local() - lastHeard > 60) {
      end("Lost the connection to the PC. The movie night may be over, or this phone is off its Wi-Fi.", false);
      return;
    }
    if (local() - lastHeard > 6) setStatus("Can't reach the PC. Trying again…", "wait");
    setTimeout(poll, 1000);
  });
}
function tell() {           // news of our own, now rather than with the next poll
  if (joined && !over) call("GET", syncPath(0), undefined, 5).then(apply, function () {});
}

// --- joining and leaving -----------------------------------------------------------------
function show(which) {
  ["join", "watch", "over"].forEach(function (id) { $(id).hidden = id !== which; });
}
function unlock() {
  // iOS lets a page play sound only from a tap: this tap starts the video and
  // stops it again, and from then on the page may start it by itself.
  if (!videoUrl && boot.video) setVideo(boot.video);
  try {
    want = true;
    var started = v.play();
    if (started && started.catch) started.catch(function () {});
  } catch (e) {}
  if (!v.paused) { want = false; v.pause(); }
  want = null;
}
var tries = 0;
function join(name, quietly) {
  if (joining) return;
  joining = true;
  if (!quietly) { $("go").disabled = true; $("again").disabled = true; $("joinerr").textContent = ""; }
  call("POST", "join", { id: me, name: name }, 10).then(function (data) {
    joining = false; tries = 0;
    $("go").disabled = false; $("again").disabled = false;
    if (data.ended && !data.joined) { end(data.ended, data.final); return; }
    joined = true; over = false; quick = 6;
    show("watch");
    apply(data);
    poll();
  }, function (e) {
    joining = false;
    $("go").disabled = false; $("again").disabled = false;
    if (e && e.status === 404) {
      end("This movie night is over. To watch the next one, scan its code on the PC.", true);
      return;
    }
    // Joining again by itself (the phone was away): for half a minute, then say so.
    if (quietly && ++tries < 15) { setTimeout(function () { join(name, true); }, 2000); return; }
    tries = 0;
    var words = "Couldn't reach the PC. Is this phone on the same Wi-Fi as it?";
    if (!$("join").hidden) $("joinerr").textContent = words;
    else end(words, false);
  });
}
function rejoin() {
  joined = false;
  st = null;
  setStatus("Joining again…", "wait");
  join(myName, true);
}
function end(reason, final) {
  // final: the movie night itself is over, and a new one has a new link.
  over = true; joined = false;
  want = false;
  if (!v.paused) v.pause();
  clearTimeout(startTimer); startTimer = null;
  $("reason").textContent = reason;
  $("again").hidden = !!final;
  show("over");
}
$("go").addEventListener("click", function () {
  myName = $("name").value.trim().slice(0, 40);
  save("name", myName);
  unlock();
  join(myName, false);
});
$("name").addEventListener("keydown", function (e) { if (e.key === "Enter") $("go").click(); });
$("again").addEventListener("click", function () {
  unlock();
  $("reason").textContent = "Joining…";
  join(myName, false);
});
$("leave").addEventListener("click", function () {
  call("POST", "bye", { id: me }, 4).catch(function () {});
  end("You left the movie night.");
});
window.addEventListener("pagehide", function () {
  if (joined && !over && navigator.sendBeacon)
    navigator.sendBeacon(BASE + "bye", new Blob([JSON.stringify({ id: me })], { type: "application/json" }));
});
window.addEventListener("pageshow", function (e) { if (e.persisted && !over && st) rejoin(); });
var hiddenTimes = 0;
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState !== "visible") { hiddenTimes++; note("hidden"); return; }
  note("visible");
  if (joined) {
    samples = []; quick = 4;     // a phone's clock stands still while it sleeps: measure again
    tell();
  }
});

// --- the video -----------------------------------------------------------------------
var want = null;            // what the page last asked of the video: true play, false pause
var seekingTo = null;       // where the page itself sent it: {t, at}
var hold = null;            // after the person's own action: leave the video until the room answers
var userBusy = 0;           // ...or until then, while it is not yet clear what they did
var seekTimer = null, fullscreenLeft = -10, loadedAt = -10;
var stalled = false, buffering = false, stalledAt = 0;
// How this phone keeps time. Never by playing faster or slower: an iPhone
// stretches the sound to keep its pitch, and its picture judders (the owner's
// first evening). A start is made on the room's clock, early by however long
// this phone takes to really start, learnt from every start. Within BAND of
// the room it is left alone; past it the picture holds until the room gets
// there, or skips ahead and waits there, and starts on the room's clock again.
var BAND = 0.2;
var ctl = { errors: [], startedAt: -10, judge: "", lead: 1.0, playLate: 0.1, held: null, lastPos: null,
            startAsked: 0, stuck: 0, skippedAt: -10, stallSeen: false };
var startTimer = null, startKey = "";
var stats = [], statsFrom = 0;

// Notes for the PC's log, sent with the next sync: what this phone really did.
var notes = [];
function note(text) { notes.push(text); if (notes.length > 12) notes.shift(); }
function ms(x) { return (x >= 0 ? "+" : "") + Math.round(x * 1000) + "ms"; }

function setVideo(url) {
  videoUrl = url;
  loading = true;
  unschedule();
  ctl.errors = []; ctl.held = null; ctl.lastPos = null; ctl.judge = "";
  var old = v.querySelectorAll("track");
  for (var i = 0; i < old.length; i++) v.removeChild(old[i]);
  trackFiles = false;
  v.src = BASE + url;
  mark();
}
function play() {
  want = true;
  if (!v.paused) return;
  var started = v.play();
  if (started && started.catch) started.catch(function (e) {
    note("play refused: " + (e && e.name));
    if (e && e.name === "NotAllowedError") $("tap").hidden = false;
  });
}
function pause() { want = false; if (!v.paused) v.pause(); }
function seekTo(t) {
  var d = st && st.media && st.media.duration;
  t = Math.max(0, d ? Math.min(t, d - 0.05) : t);
  seekingTo = { t: t, at: local() };
  v.currentTime = t;
}
function median(list) {
  var s = list.slice().sort(function (a, b) { return a - b; }), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
// Start the video `delay` s from now, less the time this phone takes to get going.
function schedule(key, delay, why) {
  if (startTimer && startKey === key) return;
  clearTimeout(startTimer);
  startKey = key;
  var seq = st.seq;
  startTimer = setTimeout(function () {
    startTimer = null; startKey = "";
    if (!st || st.seq !== seq || over || !moving(hostNow() + ctl.playLate + 0.05)) return;
    ctl.startedAt = local(); ctl.errors = []; ctl.judge = why; ctl.startAsked = local();
    ctl.stallSeen = false;
    play();
  }, Math.max(0, (delay - ctl.playLate) * 1000));
}
function unschedule() { if (startTimer) { clearTimeout(startTimer); startTimer = null; startKey = ""; } }

// The room's rules for this player, run five times a second.
function follow() {
  if (!joined || over || !st || !st.media || offset === null || loading || v.readyState < 1) return;
  if (seekTimer || local() < userBusy || (hold && local() < hold.until && st.seq === hold.seq)) return;
  // Hidden (locked, another app): an iPhone plays no video then, and a browser
  // pauses it by itself. Driving it anyway skipped it ahead every second, a
  // start that could not take each time. It catches up once when it is back.
  if (document.visibilityState !== "visible") { unschedule(); return; }
  if (Math.abs(v.playbackRate - 1) > 1e-6) v.playbackRate = 1;
  var h = hostNow(), target = positionAt(h), rate = st.rate || 1;
  if (!moving(h)) {
    // The room holds (paused, waiting for somebody, about to start): the
    // picture holds too, on the room's own frame.
    ctl.errors = [];
    pause();
    if (st.playing) schedule("start:" + st.seq, st.at - h, "start");
    else unschedule();
    if (!v.seeking) {
      var off = Math.abs(v.currentTime - target), key = st.seq + ":" + target.toFixed(3);
      var held = ctl.held && ctl.held.key === key ? ctl.held : null;
      if (off > 0.03 && (!held || (held.tries < 3 && off > 0.1 && local() - held.at > 0.4))) {
        ctl.held = { key: key, at: local(), tries: held ? held.tries + 1 : 1 };
        seekTo(target);
      }
    }
    return;
  }
  ctl.held = null;
  if (v.seeking) return;
  var pos = v.currentTime, error = pos - target;
  if (stalled) {
    // A "waiting" whose "playing" never came: the picture moving on ends it.
    var going = !v.paused && ctl.lastPos !== null && pos - ctl.lastPos > 0.1;
    ctl.lastPos = pos;
    if (!going) return;
    stalled = false; mark();
  }
  ctl.lastPos = pos;
  if (v.paused) {
    if (want === true && ctl.startAsked) {
      if (local() - ctl.startAsked < 1.0) return;       // a start on its way: iPhones take a moment
      // Asked to play a second ago and still paused. Again, without skipping;
      // after three that did not take, the person's tap (iOS may insist on one).
      ctl.startAsked = 0;
      note("start didn't take");
      if (++ctl.stuck >= 3) { ctl.stuck = 0; $("tap").hidden = false; return; }
    }
    if (error > -0.02 && error < 8) {
      // On the room's frame or ahead of it (a stream opened, a skip aimed,
      // where the room is going): start the moment the room gets here.
      schedule("arrive:" + st.seq + ":" + pos.toFixed(3), error / rate, "arrive");
      return;
    }
    // Behind: skip to where the room will be once the skip has landed, and wait
    // there. Never more than one skip in three seconds.
    unschedule();
    if (local() - ctl.skippedAt < 3) return;
    ctl.skippedAt = local();
    seekTo(target + ctl.lead * rate);
    return;
  }
  ctl.stuck = 0; ctl.startAsked = 0;
  unschedule();
  // Playing. A start is left a moment to settle before it is judged.
  if (local() - ctl.startedAt < 0.8) return;
  ctl.errors.push(error);
  if (ctl.errors.length > 5) ctl.errors.shift();
  stats.push(Math.abs(error));
  if (local() - statsFrom > 30 && stats.length > 20) {
    note("in step: median " + ms(median(stats)).slice(1) + ", worst " + ms(Math.max.apply(null, stats)).slice(1));
    stats = []; statsFrom = local();
  }
  if (ctl.errors.length < 5) return;
  var smooth = median(ctl.errors);
  if (ctl.judge) {
    // How far off this start came out: the next starts that much earlier (or
    // later). Only a clean start teaches it: one that stalled on its way (a
    // stream still being made after a far seek) came out 410 ms late, and
    // learning from that made the next starts early by as much.
    if (!ctl.stallSeen && Math.abs(smooth) < 0.25) {
      ctl.playLate = Math.min(0.5, Math.max(0, ctl.playLate - smooth * 0.8));
      note(ctl.judge + " " + ms(smooth) + ", now starts " + Math.round(ctl.playLate * 1000) + "ms early");
    } else {
      note(ctl.judge + " " + ms(smooth) + (ctl.stallSeen ? ", stalled" : "") + ", not learnt from");
    }
    ctl.judge = "";
  }
  if (Math.abs(smooth) <= BAND) return;
  // Past the band: stop, and the paused rules above land it again.
  note("fix " + ms(smooth));
  ctl.errors = [];
  pause();
}
setInterval(function () { follow(); render(); }, 200);

// What the person does with Safari's controls is the room's to do.
function intent(action, position) {
  hold = { until: local() + 1.5, seq: st ? st.seq : -1 };
  var body = { id: me, action: action };
  if (action === "seek") body.position = Math.max(0, position);
  call("POST", "do", body, 5).catch(function () { say("Couldn't reach the PC."); });
}
v.addEventListener("play", function () {
  $("tap").hidden = true;
  if (want === true || !joined || over || !st) return;
  // Pressed play: the room starts, for everyone together.
  want = false; v.pause();
  if (!st.playing) intent("play");
});
v.addEventListener("pause", function () {
  stalled = false; mark();
  if (want !== true || v.ended || !joined || over || !st) return;
  if (document.visibilityState !== "visible") return;
  // Not the page's pause: the person's, or iOS's own (the screen locking,
  // leaving full screen, a call). A moment tells them apart: the page is
  // hidden by then, or its timers stood still.
  var seen = hiddenTimes, at = local();
  userBusy = at + 0.6;
  setTimeout(function () {
    if (!v.paused || want !== true) return;
    if (hiddenTimes !== seen || document.visibilityState !== "visible" || local() - at > 1.5
        || local() - fullscreenLeft < 1.5) { userBusy = 0; return; }
    want = false;
    if (st && (st.playing || (st.waiting && st.waiting.length))) intent("pause");
  }, 350);
});
var chosen = 0;              // where the person's own seek is going
v.addEventListener("seeking", function () {
  mark();
  if (seekingTo && local() - seekingTo.at < 3 && Math.abs(v.currentTime - seekingTo.t) < 0.5) return;
  if (!joined || over || !st || local() - loadedAt < 1.5 || document.visibilityState !== "visible") return;
  // Only a real jump is the person's: Safari nudges the position by itself
  // now and then, and a PC's player ignores less than this too.
  if (!seekTimer && Math.abs(v.currentTime - positionAt(hostNow())) < 1.5) return;
  // The room holds there for everyone until they have all got there, so the
  // picture holds now: playing on through the wait put it half a second ahead.
  chosen = v.currentTime;
  if (!v.paused) { want = false; v.pause(); }
  clearTimeout(seekTimer);
  seekTimer = setTimeout(function () {       // a drag of the scrubber is one seek, where it stops
    seekTimer = null;
    intent("seek", chosen);
  }, 400);
});
v.addEventListener("seeked", function () {
  if (seekingTo && Math.abs(v.currentTime - seekingTo.t) < 0.5) {
    seekingTo = null;
    if (v.paused && st && moving(hostNow())) {
      // Aimed ctl.lead ahead of the room: landed behind it, the next aims
      // further; landed long before it arrives, a little less far.
      var early = v.currentTime - positionAt(hostNow());
      if (early < 0) ctl.lead = Math.min(6, ctl.lead - early + 0.25);
      else if (early > 1.5) ctl.lead = Math.max(0.4, ctl.lead - (early - 1.5) * 0.5);
    }
  }
  mark();
});
v.addEventListener("waiting", function () {
  if (want === true) ctl.stallSeen = true;
  if (!stalled && want === true) stalledAt = local();
  stalled = true; mark();
});
v.addEventListener("playing", function () {
  if (stalled && stalledAt && want === true) note("stalled " + Math.round((local() - stalledAt) * 1000) + "ms");
  stalled = false; stalledAt = 0; mark();
});
v.addEventListener("canplaythrough", function () { stalled = false; mark(); });
v.addEventListener("canplay", mark);
v.addEventListener("loadedmetadata", function () { loadedAt = local(); });
v.addEventListener("loadeddata", function () {
  loading = false; loadedAt = local(); mark(); follow();
  // Safari lists the playlist's subtitles by now; a browser that doesn't
  // (Chrome) gets the same files as tracks of the page's own.
  setTimeout(function () {
    if (trackFiles || subtitleTracks().length || !subtitleFiles.length) return;
    trackFiles = true;
    subtitleFiles.forEach(function (sub) {
      var track = document.createElement("track");
      track.kind = "subtitles";
      track.label = sub.name;
      if (sub.lang) track.srclang = sub.lang;
      track.src = BASE + sub.url;
      v.appendChild(track);
    });
    listSubtitles();
  }, 2000);
});
v.addEventListener("webkitendfullscreen", function () { fullscreenLeft = local(); });
v.addEventListener("error", function () {
  if (!videoUrl || over) return;
  note("video error " + (v.error ? v.error.code + " " + (v.error.message || "") : "?"));
  say("The film stopped coming through. Trying again…");
  var url = videoUrl;
  setTimeout(function () { if (videoUrl === url && !over) setVideo(url); }, 2000);
});
$("tapbutton").addEventListener("click", function () {
  $("tap").hidden = true;
  want = true;
  var started = v.play();
  if (started && started.catch) started.catch(function () { $("tap").hidden = false; });
  follow();
});
function mark() {
  var now = joined && !over && (loading || v.seeking || (stalled && want === true) || v.readyState < 2);
  if (now !== buffering) { buffering = now; tell(); }
}

// --- subtitles -------------------------------------------------------------------------
var subtitleFiles = [], trackFiles = false;
function subtitleTracks() {
  var out = [];
  for (var i = 0; v.textTracks && i < v.textTracks.length; i++) {
    var t = v.textTracks[i];
    if (t.kind === "subtitles" || t.kind === "captions") out.push(t);
  }
  return out;
}
function listSubtitles() {
  var tracks = subtitleTracks(), select = $("subs"), saved = load("subs");
  select.hidden = !tracks.length;
  select.textContent = "";
  var off = document.createElement("option");
  off.value = "-1"; off.textContent = "Subtitles off";
  select.appendChild(off);
  var chosen = -1;
  tracks.forEach(function (t, i) {
    var option = document.createElement("option");
    option.value = String(i);
    option.textContent = t.label || (t.language ? t.language.toUpperCase() : "Subtitles " + (i + 1));
    select.appendChild(option);
    if (t.mode === "showing") chosen = i;
  });
  if (saved !== null) {
    chosen = -1;
    tracks.forEach(function (t, i) { if ((t.label || t.language) === saved) chosen = i; });
    tracks.forEach(function (t, i) { t.mode = i === chosen ? "showing" : "disabled"; });
  }
  select.value = String(chosen);
}
$("subs").addEventListener("change", function () {
  var tracks = subtitleTracks(), chosen = parseInt($("subs").value, 10);
  tracks.forEach(function (t, i) { t.mode = i === chosen ? "showing" : "disabled"; });
  save("subs", chosen >= 0 ? (tracks[chosen].label || tracks[chosen].language) : "off");
});
if (v.textTracks && v.textTracks.addEventListener) {
  v.textTracks.addEventListener("addtrack", function () { setTimeout(listSubtitles, 0); });
  v.textTracks.addEventListener("removetrack", function () { setTimeout(listSubtitles, 0); });
}

// --- words --------------------------------------------------------------------------------
var newsTimer = null;
function say(text) {
  var line = $("news");
  line.textContent = text;
  line.classList.remove("gone");
  clearTimeout(newsTimer);
  newsTimer = setTimeout(function () { line.classList.add("gone"); }, 6000);
}
function setStatus(text, tone) {
  $("status").textContent = text;
  $("dot").className = "dot" + (tone ? " " + tone : "");
}
function render() {
  if (!joined || over) return;
  if (!st) { setStatus("Joining…", "wait"); return; }
  var h = hostNow();
  if (st.waiting && st.waiting.length && !st.playing) {
    setStatus("Waiting for " + andList(st.waiting.map(nameOf)) + "…", "wait");
  } else if (st.playing && h < st.at) {
    setStatus("Starting…", "on");
  } else if (st.playing) {
    setStatus(buffering ? "Loading…" : "Playing", buffering ? "wait" : "on");
  } else if (st.cause === "end") {
    setStatus("That's the end.", "");
  } else {
    setStatus("Paused", "");
  }
}
function showPeople() {
  var list = $("people");
  list.textContent = "";
  people.forEach(function (p) {
    var item = document.createElement("li"), notes = [];
    item.textContent = p.name;
    if (p.id === me) notes.push("you");
    else if (p.host) notes.push("host");
    if (p.buffering) notes.push("loading…");
    if (notes.length) {
      var note = document.createElement("span");
      note.textContent = " (" + notes.join(", ") + ")";
      item.appendChild(note);
    }
    list.appendChild(item);
  });
}

// --- looking inside: the link with #debug ------------------------------------------------------
if (/debug/.test(location.hash)) {
  window.__phone = function () {
    return { ctl: ctl, st: st, offset: offset, want: want, stalled: stalled, loading: loading,
             busyFor: userBusy - local(), seekTimer: !!seekTimer, hold: hold, startKey: startKey,
             buffering: buffering, joined: joined, now: hostNow(), target: st ? positionAt(hostNow()) : null,
             pos: v.currentTime, paused: v.paused, visible: document.visibilityState };
  };
}

// --- the first screen ------------------------------------------------------------------------
$("title").textContent = boot.title || "Movie night";
document.title = (boot.title || "Movie night") + " · Movie night";
$("with").textContent = boot.people && boot.people.length
  ? "With " + andList(boot.people) + "." : "Nobody's watching yet.";
$("name").value = myName;
if (!boot.video) {
  $("go").disabled = true;
  $("joinerr").textContent = "This PC can't make a stream a phone can play: Mistery needs ffmpeg for it.";
}
})();
</script>
</body>
</html>
"""


def render(boot: dict, nonce: str) -> str:
    """The page, with what the first screen shows (`boot`: title, people's
    names, the video's address) and the nonce the page's CSP allows."""
    data = json.dumps(boot, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e") \
        .replace("&", "\\u0026")
    return _PAGE.replace("__NONCE__", nonce).replace("__BOOT__", data)
