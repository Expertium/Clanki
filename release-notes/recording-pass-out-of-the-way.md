- The pass that records RWKV's per-review data no longer runs at start-up in
  a progress window. It waits until you leave Clanki alone, runs in the
  background, and pauses again whenever you come back, so the window never
  stops responding. While it runs, the model-quality graphs say that their
  numbers are being computed.
