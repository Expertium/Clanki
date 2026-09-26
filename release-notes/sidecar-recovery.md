- A damaged cache file beside the collection
  (`collection.retrievability-cache.sqlite`) no longer stops the collection
  from opening. Clanki moves the damaged file aside (renamed with the date,
  never deleted), makes a new one, and computes its review predictions again
  in the background. The record of which algorithm scheduled each review,
  the one part of that file nothing can compute again, now also lives in a
  small second file, `collection.scheduler-record.sqlite`, so it survives the
  damage. Clanki says something only when that record could not be
  recovered. Check Database also repairs such a file, instead of saying the
  collection is corrupt.
