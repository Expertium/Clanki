- The pass that keeps FSRS-7's per-review predictions ready no longer holds
  the collection for a whole preset while it stores its rows. It used to
  write a preset in one transaction, so a click that arrived during the
  largest preset of a big collection waited 6.0 seconds for it. The rows now
  go in in batches, the collection is free between two of them, and the
  worst click of a whole pass is under half a second. Measured on the same
  collection, the pass also finished in less CPU time than before, because a
  batch now writes one range of the cache instead of scattered rows.
- Saving a preset while its rows are being written is still safe: each batch
  checks the preset again, and a pass that finds it changed takes back the
  batches it had already written rather than leaving a mixture of old and new
  predictions in the cache.
