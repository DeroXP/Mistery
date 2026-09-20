Screenshots of the real app go here — PNG, JPEG or WebP.

The file name becomes the caption: 01-home-billboard.png shows as
"Home billboard", and the leading number decides the order. When this
folder is empty the page has no screenshots section at all, on purpose:
there is no mock-up standing in for one.

The four here:

    01-home-billboard.png      Home, billboard and Continue Watching
    02-now-playing.png         a film playing, controls up
    03-lyrics-screensaver.png  the lyrics screensaver, mid-song
    04-music-library.png       the music library, albums in a grid

They are the real app, 1600x900, taken with the window's own
QWidget.grab() (and mpv's own screenshot for the video frame, which a
grab cannot see into). The library in them is not: it is a stand-in
built for these pictures, with invented titles, artwork drawn for them
and lyrics written for them, so that a public page does not list what is
on anybody's disk. Pictures of a real library would do just as well —
these say the same thing about the app either way.

/api/health reports how many were found, so you can tell from outside
whether a deploy picked them up.
