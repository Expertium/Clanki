- Card info's forgetting curve for an RWKV-Curve card now shows the card's
  whole history: one segment per review, each one RWKV's own curve after that
  review, as FSRS-7 cards already had. Clanki saves a small source of RWKV's
  curve for every review when it reads the review history and when you
  answer a card (about 256 bytes per review, in the cache file beside the
  collection; never synced). Reviews recorded before this version, or by
  another RWKV model, get their segments once Clanki has read the history
  again, which it does by itself after start-up.
