- Clanki starts faster. The window used to wait while Python loaded `bs4`,
  `requests` and `markdown`, which are only needed to paste HTML, fetch a
  picture by URL, download an add-on or show one error message. They now load
  the first time they are used, which takes about 80 ms off every start.
- Closing a dialog no longer makes the window pause. Clanki cleans up unused
  memory a second after a dialog closes and every 15 minutes; that clean-up
  walked everything the program had loaded, and took 17-35 ms each time. It now
  skips the parts that never change, and takes under a millisecond.
