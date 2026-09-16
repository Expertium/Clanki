- Closing a window (Stats, the Browser, deck options, ...) no longer makes
  Anki pause a second later: the garbage collection that follows a dialog
  close, and the one every 15 minutes, now skip everything the program built
  while it started (its modules, add-ons and main window), which never
  becomes garbage anyway.
