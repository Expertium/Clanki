- AnkiConnect is built in: Yomitan, browser extensions, Obsidian plugins and
  scripts that talk to the AnkiConnect add-on work with Clanki without it,
  including the actions of the AnkiConnect Extended fork (cardsDetails,
  gradeNow, repositionNewCards, guiAddNoteSetData, guiPlayAudio, and R/S/D
  fields in cardsInfo and findCards). Turn it on in Preferences >
  AnkiConnect (address, port, allowed web origins, API key); it is off by
  default. An enabled AnkiConnect add-on is disabled at start-up and its
  settings carried over, with AnkiConnect left on; installing or enabling
  the add-on turns the built-in AnkiConnect on instead.
- Requests no longer hold up the window: the connections are served off the
  main thread, collection work runs off it too, and the screens refresh for
  what a request changed instead of resetting after each one.
- Scheduling values over AnkiConnect follow the collection's algorithm, as
  card info shows them; RWKV-Curve cards are answered only in the reviewer.
- If the port is in use (another Anki with AnkiConnect, for example),
  Clanki says so once and keeps trying instead of showing an error box at
  start-up.
