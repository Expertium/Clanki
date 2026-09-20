- The pass that records RWKV's per-review data now goes on while you study.
  It used to wait for ten seconds of quiet between two batches, and every key
  press started that wait again, so on a large collection it made no progress
  at all during a review session: measured, 0 of 656,402 reviews in five
  minutes of use, for a job of two minutes. It now works in short batches and
  rests a moment between two of them, and the whole pass finishes in minutes
  of study. Because it now finishes, RWKV-Curve's per-review predictions are
  recorded, so RWKV-Curve appears in the AUC-ROC and calibration graphs
  instead of being greyed out.
- While it rests, the pass hands the RWKV model back, so answering a card,
  showing one or an undo waits for one batch at most instead of for the whole
  pass. Measured on a 656,000-review collection: more than 30 seconds before,
  a third of a second at worst now.
