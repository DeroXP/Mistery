/* The phone remote: browse a Mistery library and play it here, or on the PC.

   Everything comes from the owner's PC through this site's relay (relay.py):
   /r/<id>/api/... for the library and commands, each request signed with the
   key from the link's #fragment (HMAC-SHA256; the key never leaves this phone),
   and /r/<id>/m/<token>/... for pictures, songs and films, by short-lived
   tokens the PC hands out. No cookies; the key is kept in this phone's
   localStorage for this PC only. Built without a framework, from DOM nodes and
   textContent, so nothing the PC sends is ever read as HTML. */
"use strict";

(() => {
  const RID = document.body.dataset.remote;
  const BASE = `/r/${RID}`;
  const STORE = `mistery-remote-${RID}`;
  const view = document.getElementById("view");
  const mini = document.getElementById("mini");
  const sheet = document.getElementById("sheet");
  const screen = document.getElementById("screen");
  const audio = document.getElementById("audio");
  const dot = document.getElementById("pc-dot");
  const pcName = document.getElementById("pc-name");

  // --- the key ------------------------------------------------------------------------------

  let keyHex = null;
  const fromLink = location.hash.slice(1).toLowerCase();
  if (/^[0-9a-f]{32}$/.test(fromLink)) {
    keyHex = fromLink;
    try { localStorage.setItem(STORE, keyHex); } catch (e) { /* private browsing: kept for this visit */ }
    history.replaceState(null, "", location.pathname);    // out of the address bar and the history
  } else {
    try { keyHex = localStorage.getItem(STORE); } catch (e) { keyHex = null; }
  }

  let keyPromise = null;
  function cryptoKey() {
    if (!keyPromise) {
      const bytes = new Uint8Array(keyHex.match(/../g).map((h) => parseInt(h, 16)));
      keyPromise = crypto.subtle.importKey("raw", bytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    }
    return keyPromise;
  }

  const enc = new TextEncoder();
  const hex = (buffer) => Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
  let skew = 0;                       // the PC's clock minus this phone's, in seconds

  async function signed(method, path, body) {
    const time = String(Math.floor(Date.now() / 1000) + skew);
    const nonce = hex(crypto.getRandomValues(new Uint8Array(8)));
    const bodyHash = hex(await crypto.subtle.digest("SHA-256", enc.encode(body || "")));
    const text = `${method}\n${path}\n${time}\n${nonce}\n${bodyHash}`;
    const sig = hex(await crypto.subtle.sign("HMAC", await cryptoKey(), enc.encode(text)));
    return { "X-Mistery-Time": time, "X-Mistery-Nonce": nonce, "X-Mistery-Sig": sig };
  }

  class Problem extends Error {
    constructor(status, words) { super(words); this.status = status; }
  }

  async function api(method, path, payload, retried) {
    const body = payload === undefined ? "" : JSON.stringify(payload);
    const headers = await signed(method, path, body);
    if (body) headers["Content-Type"] = "application/json";
    let answer;
    try {
      answer = await fetch(BASE + path, { method, headers, body: body || undefined, cache: "no-store" });
    } catch (e) {
      setConnected(false);
      throw new Problem(0, "No connection. Check your phone's internet.");
    }
    let data = null;
    try { data = await answer.json(); } catch (e) { data = null; }
    if (answer.status === 401 && data && typeof data.now === "number" && !retried) {
      skew = data.now - Math.floor(Date.now() / 1000);       // this phone's clock is off: once more
      return api(method, path, payload, true);
    }
    if (!answer.ok) {
      if (answer.status === 503 || answer.status === 504) setConnected(false);
      throw new Problem(answer.status, (data && data.error) || `Something went wrong (${answer.status}).`);
    }
    setConnected(true);
    return data;
  }

  // --- little helpers -----------------------------------------------------------------------

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (name === "class") node.className = value;
      else if (name === "text") node.textContent = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else if (name === "style") node.setAttribute("style", value);
      else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }

  function length(seconds) {
    if (!seconds) return "";
    const m = Math.round(seconds / 60);
    return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m} min`;
  }

  let toastTimer = null;
  function toast(words) {
    document.querySelectorAll(".toast").forEach((n) => n.remove());
    const node = el("div", { class: "toast", text: words });
    document.body.append(node);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.remove(), 3200);
  }

  function setConnected(ok) {
    dot.className = "dot " + (ok ? "ok" : "bad");
  }

  let artBase = null;
  function artUrl(kind, id) {
    return artBase ? `${artBase}/${kind}/${id}` : null;
  }

  function art(kind, item, letter) {
    const node = el("div", { class: "art" });
    const url = item && item.art ? artUrl(kind, item.id) : null;
    if (url) node.style.backgroundImage = `url("${url}")`;
    else node.textContent = (letter || (item && item.title) || "?").trim().charAt(0).toUpperCase();
    return node;
  }

  function choose(title, options) {
    sheet.replaceChildren(el("div", { class: "box" },
      el("div", { class: "title", text: title }),
      options.map(([words, act]) => el("button", { text: words, onclick: () => { close(); act(); } })),
      el("button", { text: "Cancel", class: "dim", onclick: () => close() })));
    sheet.hidden = false;
    function close() { sheet.hidden = true; sheet.replaceChildren(); }
    sheet.onclick = (event) => { if (event.target === sheet) close(); };
  }

  // --- pages ----------------------------------------------------------------------------------

  let tab = "music";
  let musicMode = "albums";
  let library = null;
  const stack = [];                     // pages opened on top of the tab's own, for Back

  function show(build) {
    view.replaceChildren(el("p", { class: "note", text: "Loading…" }));
    view.scrollTop = 0;
    Promise.resolve().then(build).then((nodes) => {
      if (nodes) view.replaceChildren(...[].concat(nodes));
    }).catch((problem) => {
      view.replaceChildren(trouble(problem));
    });
  }

  function trouble(problem) {
    const words = problem && problem.message ? problem.message : String(problem);
    const box = el("div", { class: "note" }, el("p", { text: words }));
    if (problem && problem.status === 401) {
      box.append(el("p", { class: "faint", text: "Link this phone again: on your PC, Settings, then Phone remote, then Link a phone." }));
    } else {
      box.append(el("button", { class: "btn", text: "Try again", onclick: () => start() }));
    }
    return box;
  }

  function open(build) {
    stack.push(build);
    show(build);
  }

  function back() {
    stack.pop();
    show(stack.length ? stack[stack.length - 1] : tabPage);
  }

  function backButton() {
    return el("button", { class: "back", text: "‹ Back", onclick: back });
  }

  async function loadLibrary(force) {
    if (!library || force) library = await api("GET", "/api/library");
    return library;
  }

  function tabPage() {
    if (tab === "music") return musicPage();
    if (tab === "films") return filmsPage();
    if (tab === "shows") return showsPage();
    if (tab === "lists") return listsPage();
    return remotePage();
  }

  async function musicPage() {
    const lib = await loadLibrary();
    const chips = el("div", { class: "chips" }, [["albums", "Albums"], ["liked", "Liked"], ["songs", "Songs"]].map(
      ([mode, words]) => el("button", { class: "chip" + (mode === musicMode ? " on" : ""), text: words,
        onclick: () => { musicMode = mode; show(musicPage); } })));
    if (musicMode === "liked") {
      const data = await api("GET", "/api/liked");
      return [el("h1", { text: "Music" }), chips, songList(data.songs, "Liked songs", true)];
    }
    if (musicMode === "songs") {
      const field = el("input", { class: "find", type: "search", placeholder: "Find a song, artist or album", autocomplete: "off" });
      const results = el("div", { class: "list" });
      let timer = null;
      field.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const q = field.value.trim();
          if (!q) { results.replaceChildren(); return; }
          try {
            const data = await api("GET", `/api/songs?q=${encodeURIComponent(q)}`);
            results.replaceChildren(songList(data.songs, `Songs matching "${q}"`, true));
          } catch (problem) { results.replaceChildren(trouble(problem)); }
        }, 300);
      });
      setTimeout(() => field.focus(), 50);
      return [el("h1", { text: "Music" }), chips, field, results];
    }
    if (!lib.albums.length) return [el("h1", { text: "Music" }), chips, el("p", { class: "note", text: "No music on your PC yet." })];
    return [el("h1", { text: "Music" }), chips, el("div", { class: "grid" }, lib.albums.map((album) =>
      el("button", { class: "tile", onclick: () => open(() => albumPage(album.id)) },
        art("album", album), el("div", { class: "t", text: album.title }),
        el("div", { class: "s", text: [album.artist, album.year].filter(Boolean).join(" · ") }))))];
  }

  function songList(songs, label, numbered) {
    if (!songs.length) return el("p", { class: "note", text: "Nothing here." });
    return el("div", { class: "list" }, songs.map((song, index) => {
      const row = el("div", { class: "row" + (queue[position] && queue[position].id === song.id ? " playing" : "") },
        numbered ? el("span", { class: "n", text: String(index + 1) }) : null,
        el("div", { class: "body", onclick: () => playHere(songs, index, label) },
          el("div", { class: "t", text: song.title }),
          el("div", { class: "s", text: [song.artist, song.album, clock(song.duration)].filter(Boolean).join(" · ") })),
        el("button", { class: "more", text: "⋯", "aria-label": "More", onclick: () => choose(song.title, [
          ["Play on this phone", () => playHere(songs, index, label)],
          ["Play on my PC", () => playOnPc({ kind: "track", id: song.id })],
        ]) }));
      return row;
    }));
  }

  async function albumPage(id) {
    const data = await api("GET", `/api/album/${id}`);
    const album = data.album;
    return [backButton(),
      el("div", { class: "hero" }, art("album", album),
        el("div", { class: "words" }, el("h1", { text: album.title }),
          el("div", { class: "dim", text: [album.artist, album.year].filter(Boolean).join(" · ") }))),
      el("div", { class: "actions" },
        el("button", { class: "btn main", text: "Play here", onclick: () => playHere(data.songs, 0, album.title) }),
        el("button", { class: "btn", text: "Play on my PC", onclick: () => playOnPc({ kind: "album", id: album.id }) })),
      songList(data.songs, album.title, true)];
  }

  async function filmsPage() {
    const lib = await loadLibrary();
    if (!lib.movies.length) return [el("h1", { text: "Films" }), el("p", { class: "note", text: "No films on your PC yet." })];
    return [el("h1", { text: "Films" }), el("div", { class: "grid posters" }, lib.movies.map((film) =>
      el("button", { class: "tile", onclick: () => open(() => filmPage(film)) },
        art("movie", film),
        film.progress > 0 && !film.watched ? el("div", { class: "bar" }, el("i", { style: `width:${Math.round(film.progress * 100)}%` })) : null,
        el("div", { class: "t", text: film.title }),
        el("div", { class: "s", text: [film.year, length(film.duration), film.watched ? "watched" : ""].filter(Boolean).join(" · ") }))))];
  }

  function filmPage(film) {
    return [backButton(),
      el("div", { class: "hero poster" }, art("movie", film),
        el("div", { class: "words" }, el("h1", { text: film.title }),
          el("div", { class: "dim", text: [film.year, length(film.duration)].filter(Boolean).join(" · ") }))),
      el("div", { class: "actions" },
        el("button", { class: "btn main", text: film.progress > 0 && !film.watched ? "Resume here" : "Play here",
          onclick: () => watchHere({ kind: "movie", id: film.id, title: film.title }) }),
        el("button", { class: "btn", text: "Play on my PC", onclick: () => playOnPc({ kind: "movie", id: film.id }) })),
      film.overview ? el("p", { class: "overview", text: film.overview }) : null];
  }

  async function showsPage() {
    const lib = await loadLibrary();
    if (!lib.shows.length) return [el("h1", { text: "Shows" }), el("p", { class: "note", text: "No shows on your PC yet." })];
    return [el("h1", { text: "Shows" }), el("div", { class: "grid posters" }, lib.shows.map((series) =>
      el("button", { class: "tile", onclick: () => open(() => showPage(series.id)) },
        art("show", series), el("div", { class: "t", text: series.title }),
        el("div", { class: "s", text: [series.year, `${series.episodes} episode${series.episodes === 1 ? "" : "s"}`].filter(Boolean).join(" · ") }))))];
  }

  async function showPage(id) {
    const data = await api("GET", `/api/show/${id}`);
    const nodes = [backButton(), el("div", { class: "hero poster" }, art("show", data.show),
      el("div", { class: "words" }, el("h1", { text: data.show.title }),
        el("div", { class: "dim", text: `${data.episodes.length} episodes` })))];
    let season = null;
    let list = null;
    for (const episode of data.episodes) {
      if (episode.season !== season) {
        season = episode.season;
        nodes.push(el("h2", { text: season ? `Season ${season}` : "Specials" }));
        list = el("div", { class: "list" });
        nodes.push(list);
      }
      list.append(el("div", { class: "row" },
        el("span", { class: "n", text: episode.episode != null ? String(episode.episode) : "" }),
        el("div", { class: "body", onclick: () => choose(episode.title, [
          ["Play on this phone", () => watchHere({ kind: "episode", id: episode.id, title: `${data.show.title}: ${episode.title}` })],
          ["Play on my PC", () => playOnPc({ kind: "episode", id: episode.id })],
        ]) },
          el("div", { class: "t", text: episode.title }),
          el("div", { class: "s", text: [length(episode.duration), episode.watched ? "watched" :
            episode.progress > 0 ? `${Math.round(episode.progress * 100)}% watched` : ""].filter(Boolean).join(" · ") }))));
    }
    return nodes;
  }

  async function listsPage() {
    const lib = await loadLibrary();
    const sections = [el("h1", { text: "Playlists" })];
    for (const [kind, words] of [["music", "Music"], ["video", "Films and episodes"]]) {
      const lists = lib.playlists[kind] || [];
      if (!lists.length) continue;
      sections.push(el("h2", { text: words }), el("div", { class: "list" }, lists.map((list) =>
        el("button", { class: "row", onclick: () => open(() => playlistPage(kind, list.id)) },
          el("div", { class: "body" }, el("div", { class: "t", text: list.name }),
            el("div", { class: "s", text: `${list.count} item${list.count === 1 ? "" : "s"}` }))))));
    }
    if (sections.length === 1) sections.push(el("p", { class: "note", text: "No playlists yet." }));
    return sections;
  }

  async function playlistPage(kind, id) {
    const data = await api("GET", `/api/playlist/${kind}/${id}`);
    const head = [backButton(), el("h1", { text: data.name })];
    if (kind === "music") {
      return [...head, el("div", { class: "actions" },
        el("button", { class: "btn main", text: "Play here", onclick: () => playHere(data.songs, 0, data.name) }),
        el("button", { class: "btn", text: "Play on my PC", onclick: () => playOnPc({ kind: "playlist", id }) })),
        songList(data.songs, data.name, true)];
    }
    return [...head, el("div", { class: "actions" },
      el("button", { class: "btn", text: "Play on my PC", onclick: () => playOnPc({ kind: "playlist", id }) })),
      el("div", { class: "list" }, data.items.map((item) => el("div", { class: "row" },
        el("div", { class: "body", onclick: () => choose(item.title, [
          ["Play on this phone", () => watchHere({ kind: item.kind, id: item.id, title: item.title })],
          ["Play on my PC", () => playOnPc({ kind: item.kind, id: item.id })],
        ]) }, el("div", { class: "t", text: item.title }),
          el("div", { class: "s", text: [item.year, length(item.duration)].filter(Boolean).join(" · ") })))))];
  }

  // --- the PC as the player --------------------------------------------------------------------

  async function playOnPc(what) {
    try {
      const done = await api("POST", "/api/play", what);
      toast(done.said || "Playing on your PC.");
    } catch (problem) { toast(problem.message); }
  }

  let nowTimer = null;
  async function remotePage() {
    const now = await api("GET", "/api/now");
    const nodes = [el("h1", { text: "On my PC" })];
    if (!now.window) {
      nodes.push(el("p", { class: "note", text: "Mistery's window isn't open on your PC, so there is nothing to control. You can still play anything on this phone." }));
      return nodes;
    }
    nodes.push(controlCard("Music", "music", now.music), controlCard("Film", "video", now.video));
    clearInterval(nowTimer);
    nowTimer = setInterval(async () => {
      if (tab !== "remote" || stack.length || document.hidden) return;
      try {
        const fresh = await api("GET", "/api/now");
        const cards = view.querySelectorAll(".card");
        if (cards.length === 2) {
          cards[0].replaceWith(controlCard("Music", "music", fresh.music));
          cards[1].replaceWith(controlCard("Film", "video", fresh.video));
        }
      } catch (problem) { /* the dot says so */ }
    }, 2500);
    return nodes;
  }

  function controlCard(label, target, state) {
    const card = el("div", { class: "card" }, el("div", { class: "label", text: label }));
    if (!state) {
      card.append(el("p", { class: "dim", text: target === "music" ? "No music playing." : "No film playing." }));
      return card;
    }
    const doIt = (action, value) => api("POST", "/api/do", { target, action, value }).catch((p) => toast(p.message));
    const seek = el("input", { class: "seek", type: "range", min: 0, max: Math.max(1, Math.floor(state.duration || 0)),
      value: Math.floor(state.position || 0) });
    seek.addEventListener("change", () => doIt("seek", Number(seek.value)));
    card.append(
      el("div", { class: "t", style: "font-weight:700;font-size:17px;margin-top:6px", text: state.title }),
      el("div", { class: "dim", text: state.subtitle || "" }),
      seek, el("div", { class: "times" }, el("span", { text: clock(state.position) }), el("span", { text: clock(state.duration) })),
      el("div", { class: "transport" },
        el("button", { text: "⏮", "aria-label": "Previous", onclick: () => doIt("previous") }),
        el("button", { class: "big", text: state.playing ? "❚❚" : "▶", "aria-label": "Play or pause", onclick: () => doIt("toggle") }),
        el("button", { text: "⏭", "aria-label": "Next", onclick: () => doIt("next") })));
    if (typeof state.volume === "number") {
      const volume = el("input", { type: "range", min: 0, max: 100, value: Math.round(state.volume) });
      volume.addEventListener("change", () => doIt("volume", Number(volume.value)));
      card.append(el("div", { class: "volume" }, el("span", { text: "🔈" }), volume, el("span", { text: "🔊" })));
    }
    return card;
  }

  // --- this phone as the player: music -----------------------------------------------------------

  let queue = [];
  let position = -1;
  let queueName = "";

  async function playHere(songs, index, label) {
    queue = songs.slice();
    queueName = label || "";
    await playAt(index);
  }

  async function playAt(index) {
    if (index < 0 || index >= queue.length) return;
    position = index;
    const song = queue[index];
    drawMini();
    try {
      const stream = await api("POST", "/api/stream", { kind: "track", id: song.id });
      audio.src = BASE + stream.url;
      await audio.play();
    } catch (problem) {
      toast(problem.message || "That song couldn't be played.");
    }
    sessionInfo(song);
    drawMini();
  }

  audio.addEventListener("ended", () => { if (position + 1 < queue.length) playAt(position + 1); else drawMini(); });
  audio.addEventListener("play", drawMini);
  audio.addEventListener("pause", drawMini);
  audio.addEventListener("error", () => { if (audio.src) toast("That song stopped. Your PC may have gone offline."); });

  function sessionInfo(song) {
    if (!("mediaSession" in navigator)) return;
    const url = song.album_id && song.art ? artUrl("album", song.album_id) : null;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: song.title, artist: song.artist || "", album: song.album || queueName,
      artwork: url ? [{ src: new URL(url, location.href).href, sizes: "360x360", type: "image/jpeg" }] : [],
    });
    navigator.mediaSession.setActionHandler("play", () => audio.play());
    navigator.mediaSession.setActionHandler("pause", () => audio.pause());
    navigator.mediaSession.setActionHandler("nexttrack", () => playAt(position + 1));
    navigator.mediaSession.setActionHandler("previoustrack", () => (audio.currentTime > 3 ? (audio.currentTime = 0) : playAt(position - 1)));
    try {
      navigator.mediaSession.setActionHandler("seekto", (details) => { audio.currentTime = details.seekTime; });
    } catch (e) { /* not every browser */ }
  }

  function drawMini() {
    const song = queue[position];
    if (!song) { mini.hidden = true; return; }
    mini.hidden = false;
    const thumb = song.album_id && song.art ? el("div", { class: "art thumb", style: `background-image:url("${artUrl("album", song.album_id)}")` })
      : el("div", { class: "art thumb", text: (song.title || "?").charAt(0) });
    mini.replaceChildren(thumb,
      el("div", { class: "body", onclick: () => open(playerPage) },
        el("div", { class: "t", text: song.title }), el("div", { class: "s", text: song.artist || queueName })),
      el("button", { class: "ctl", text: audio.paused ? "▶" : "❚❚", "aria-label": "Play or pause",
        onclick: () => (audio.paused ? audio.play() : audio.pause()) }),
      el("button", { class: "ctl", text: "⏭", "aria-label": "Next", onclick: () => playAt(position + 1) }));
  }

  function playerPage() {
    const song = queue[position];
    if (!song) return [backButton(), el("p", { class: "note", text: "Nothing playing on this phone." })];
    const seek = el("input", { class: "seek", type: "range", min: 0, max: Math.max(1, Math.floor(audio.duration || song.duration || 1)), value: Math.floor(audio.currentTime) });
    const times = el("div", { class: "times" }, el("span", { text: clock(audio.currentTime) }), el("span", { text: clock(audio.duration || song.duration) }));
    seek.addEventListener("input", () => { audio.currentTime = Number(seek.value); });
    const tick = setInterval(() => {
      if (!document.body.contains(seek)) { clearInterval(tick); return; }
      seek.max = Math.max(1, Math.floor(audio.duration || song.duration || 1));
      seek.value = Math.floor(audio.currentTime);
      times.firstChild.textContent = clock(audio.currentTime);
    }, 500);
    const playButton = el("button", { class: "big", text: audio.paused ? "▶" : "❚❚", onclick: () => {
      if (audio.paused) audio.play(); else audio.pause();
      playButton.textContent = audio.paused ? "▶" : "❚❚";
    } });
    const cover = el("div", { class: "art" });
    if (song.album_id && song.art) cover.style.backgroundImage = `url("${artUrl("album", song.album_id)}")`;
    return [backButton(), el("div", { class: "player" }, cover,
      el("h1", { text: song.title }), el("div", { class: "s", text: [song.artist, song.album].filter(Boolean).join(" · ") }),
      seek, times,
      el("div", { class: "transport" },
        el("button", { text: "⏮", onclick: () => playAt(position - 1).then(() => show(playerPage)) }),
        playButton,
        el("button", { text: "⏭", onclick: () => playAt(position + 1).then(() => show(playerPage)) })),
      el("p", { class: "faint", text: queueName ? `Playing from ${queueName}` : "" }))];
  }

  // --- this phone as the player: films -----------------------------------------------------------

  async function watchHere(what) {
    const probe = document.createElement("video");
    if (!probe.canPlayType("application/vnd.apple.mpegurl")) {
      toast("This browser can't play films here. Safari on iPhone and Chrome on Android can.");
      return;
    }
    let stream;
    try {
      stream = await api("POST", "/api/stream", { kind: what.kind, id: what.id });
    } catch (problem) { toast(problem.message); return; }
    if (!audio.paused) audio.pause();
    const video = el("video", { controls: true, playsinline: true, autoplay: true, preload: "auto" });
    video.src = BASE + stream.url;
    if (stream.start > 0) {
      video.addEventListener("loadedmetadata", () => { try { video.currentTime = stream.start; } catch (e) { /* later */ } }, { once: true });
    }
    let saved = 0;
    const save = (final) => {
      if (!video.currentTime || (!final && Math.abs(video.currentTime - saved) < 10)) return;
      saved = video.currentTime;
      api("POST", "/api/progress", { kind: what.kind, id: what.id, position: video.currentTime,
        duration: video.duration || stream.duration || 0 }).catch(() => {});
    };
    const timer = setInterval(() => save(false), 15000);
    const closeIt = () => {
      save(true);
      clearInterval(timer);
      video.pause();
      video.removeAttribute("src");
      video.load();
      screen.hidden = true;
      screen.replaceChildren();
    };
    screen.replaceChildren(el("button", { class: "close", text: "Close", onclick: closeIt }), video);
    screen.hidden = false;
    video.play().catch(() => { /* the controls are there to press */ });
  }

  // --- starting ---------------------------------------------------------------------------------

  document.getElementById("tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    tab = button.dataset.tab;
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b === button));
    stack.length = 0;
    show(tabPage);
  });

  async function start() {
    if (!keyHex) {
      pcName.textContent = "Not linked";
      setConnected(false);
      view.replaceChildren(el("div", { class: "note" },
        el("p", { text: "This phone isn't linked to a PC yet." }),
        el("p", { class: "faint", text: "On your PC, open Mistery, go to Settings, then Phone remote, and choose Link a phone. Open the link it gives you here." })));
      return;
    }
    if (!window.crypto || !crypto.subtle) {
      view.replaceChildren(el("p", { class: "note", text: "This browser can't sign requests here. Open the link in Safari or Chrome." }));
      return;
    }
    try {
      const hello = await api("GET", "/api/hello");
      pcName.textContent = hello.name || "Your PC";
      artBase = hello.art ? BASE + hello.art : null;
      library = null;
      show(tabPage);
    } catch (problem) {
      pcName.textContent = problem.status === 401 ? "Link expired" : "Can't reach your PC";
      view.replaceChildren(trouble(problem));
    }
  }

  // Pictures come by a token that lasts hours; a phone left on overnight asks again.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && keyHex) {
      api("GET", "/api/hello").then((hello) => { artBase = hello.art ? BASE + hello.art : artBase; }).catch(() => {});
    }
  });

  start();
})();
