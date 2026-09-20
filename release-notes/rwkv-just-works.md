- RWKV does its own upkeep. When the Stats graphs' per-review RWKV data or
  card info's saved RWKV curves are missing, or were made by another RWKV
  model, Clanki records them again by itself, once, in the "Preparing Stats"
  window, after start-up and never while you review. After a sync that
  brings reviews older than 8 days, Clanki reads the whole review history
  again by itself instead of asking you to press "Read Review History
  Again"; the warning about such reviews is gone.
