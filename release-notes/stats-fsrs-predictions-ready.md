- FSRS-7's per-review predictions are now kept ready by Clanki itself, so the
  model-quality graphs on the Stats page find them instead of showing FSRS-7 as
  an algorithm with nothing to draw. There is no button to find and nothing to
  start: the pass runs off the main thread once after the collection opens, at
  most once a day, and the Stats page never waits for it.
- When a preset's FSRS-7 parameters change, every prediction those parameters
  produced is wrong, so Clanki deletes that preset's stored predictions with the
  change and writes them again at once. Only that preset is affected; the other
  presets' predictions were made by parameters that did not change. Desired
  retention, easy days and fuzz change the schedule rather than the prediction,
  and delete nothing.
- While the predictions are being written, the graphs say so under FSRS-7's
  name rather than calling it absent, and they never draw a value that the
  current parameters did not produce.
