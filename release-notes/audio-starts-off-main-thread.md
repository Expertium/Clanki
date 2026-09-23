- Clanki no longer waits for its sound player when a profile opens: the main
  window shows about 0.1 s sooner, and up to 10 s sooner on a busy machine,
  where the bundled mpv can hang at start. A sound asked for in that moment
  plays as soon as the player is ready.
