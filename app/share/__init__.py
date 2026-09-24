"""Library sharing: friends' films, shows and music, played from their PC.

Movie night (app/party) is two people watching one thing together for an
evening. This is the other half: two Misterys that know each other, so either
can browse what the other has and play any of it, whenever the other's PC is on.

    identity.py   who this Mistery is: one key and certificate that last, kept
                  in the data folder, and the mutual TLS both sides check
    pairing.py    adding a friend: the one-time code, and the exchange that
                  leaves each side holding the other's certificate and name
    catalog.py    what this library holds, sent to a friend and kept by them so
                  they can see it while this PC is off
    server.py     the routes a friend may ask for, on the movie night listener
    sharer.py     serving while Mistery is closed, and keeping the PC awake
                  while a friend is actually watching

Nothing here is a server on the internet: a friend's Mistery connects straight
to this PC, the same way a movie night guest does, over the same one port.
"""
