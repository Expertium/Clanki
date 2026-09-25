- "Undo Answer Card" and "Undo Grade Now" are no longer lost after the first
  answer of a new card under RWKV. Clanki's read of the card's review history
  was taken for a write, because it did not start with SELECT, and every
  write drops the undo step and the study queue. Clanki now asks SQLite
  whether a statement writes anything, so reads keep the undo step and the
  queue, and a write that starts with WITH still drops them.
