- The pass that records RWKV's per-review numbers no longer takes the one
  worker that answering a card, clicking a deck, the deck list, the Browser
  and the Stats all share. It runs on a thread of its own, so Clanki stays
  usable while it works. On a 656,000-review collection that pass takes about
  45 minutes, and everything above it used to wait for the whole of it.
