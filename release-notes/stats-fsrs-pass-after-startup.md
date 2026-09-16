- The pass that writes FSRS-7's per-review predictions now waits for the RWKV
  state cache to finish loading before it starts, and recomputes one preset at
  a time with the collection free in between. Before this, on the first start-up
  after the feature arrived, the whole backfill ran ahead of the state-cache
  load and held the collection for all of it, so the deck list and everything
  else waited for a backfill and then for a restore.
- The pass shows no progress of its own and clears none, so it cannot disturb
  what the window is already showing. While it runs, the only sign of it is the
  model-quality graphs saying that FSRS-7's predictions are being computed.
