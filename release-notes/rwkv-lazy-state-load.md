- Opening a profile no longer reads the whole RWKV state cache. Clanki now
  reads a card's or note's stored state when that card comes up, so the
  start-up progress window closes in a fraction of the time. A cache written
  by an older Clanki is converted once, at the first profile open after the
  update; this takes a few minutes for a large cache and replays no reviews.
