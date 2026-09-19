"""Movie night: watching one film or episode together, from the host's library.

The host's Mistery opens one port (asking the router to forward it), makes a
certificate for this session alone, and hands out an invite code that carries
the addresses, the port, a secret token and the certificate's pin. A guest's
Mistery connects with that code, checks the pin before sending a byte, and plays
the stream through a small proxy on its own machine. Play, pause and seek from
anybody go through the host, which keeps everyone together.

Where the party got to is the party's own business: it is written to
party_progress on every side, and never to anybody's own progress. Watching
episode 3 with friends does not move you back from episode 7.

    invite      the code: what it carries, and reading it back out of a paste
    upnp        asking the router for a port, and giving it back
    stun        this PC's internet address, when the router won't say
    tls         the session's certificate, and a guest's pinned connection
    server      the one listener: the media over HTTP, and the sync channel
    transcode   ffmpeg, for friends whose connection cannot take the original
    guest_proxy what a guest's mpv actually plays from
    people      who you are: a stable id and the name friends see
    sync        the room: state, intents, clocks and drift
    session     all of the above, for the Qt side
"""
