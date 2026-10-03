- Clanki now uses the latest fsrs-rs, which computes FSRS-7 without the Burn
  library. Answering a card and recomputing memory states (after a parameter
  change, a reschedule or in card info) are 10 to 15 times faster. Intervals
  and memory states stay the same apart from float rounding (a very small
  number of long intervals move by one day), and optimized parameters fit
  your reviews as well as before.
- Optimizing FSRS-7 parameters is about 1.6 times faster: fsrs-rs trains
  FSRS-7 as the FSRS benchmark does, on two threads. The new parameters are
  slightly different; Clanki still keeps your current ones when they fit
  your reviews better.
