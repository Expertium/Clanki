- Closing Clanki no longer freezes the window for several seconds. The
  closing work (the collection check, the backup and the close) now runs in
  the background, and the check skips Clanki's large prediction cache. On a
  copy of a large collection, the longest freeze while closing went from
  3.7-8.2 s to under 0.02 s.
