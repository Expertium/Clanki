- Opening a profile no longer reads the whole RWKV state cache. Clanki now
  reads a card's or note's stored state when that card comes up, so the
  start-up progress window closes in a fraction of the time. A cache written
  by an older Clanki is converted once, at the first profile open after the
  update, behind a window that says so; this takes about 16 seconds for a
  3.5 GB cache and replays no reviews.
- Going back to an older Clanki after the conversion rejects the converted
  cache and builds a new one from scratch. Nothing is lost; the rebuild costs
  the usual one-off wait.
