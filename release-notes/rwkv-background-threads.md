- RWKV's long background passes (rebuilding the RWKV states, recomputing the
  calibration data, and the Total Knowledge graph) now use at most a quarter of
  the computer's processor threads, up to 8, instead of all of them. The rest of
  the computer stays responsive while they run; the passes take somewhat longer.
