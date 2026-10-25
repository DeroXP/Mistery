"""The phone remote: this library on its owner's phone, from anywhere.

The owner asked (2026-10-25) for "a section of the website where one's phone can
control their library remotely and play and listen to whatever they want", and
chose: both (remote control and playing on the phone), anywhere through the
website, music and films.

    link.py    the secret that proves this PC to the website, and the phone's key
    relay.py   the one connection this PC keeps open to the website
    api.py     what the phone may ask, checked against its key: the library,
               pictures, a song or a film made for a phone, and commands
    bridge.py  commands that need Mistery's window (play this on the PC, pause)

The website passes requests through and keeps nothing (server/relay.py). It
never has the phone's key, so it cannot ask this PC for anything itself.
"""
