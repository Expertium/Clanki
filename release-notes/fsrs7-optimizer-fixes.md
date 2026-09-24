- Optimizing FSRS-7 parameters (automatic, or "Optimize All Presets") keeps a
  preset's current parameters when the new ones do not predict its reviews
  better, and never replaces trained parameters with untrained values from a
  handful of reviews.
- The FSRS-7 series in the Stats model-quality graphs is fitted on the same
  reviews as the preset's parameters: suspended cards, the preset's search
  filter and "Ignore reviews before" now count there too.
- One preset that cannot be optimized (for example an invalid search filter)
  no longer stops the automatic optimization, the Stats predictions or
  "Optimize All Presets" for the other presets.
- The daily FSRS-7 prediction pass no longer fits every preset again each day:
  a preset with no new review and no changed setting is left as it is.
- The FSRS simulator starts each existing card from its real FSRS-7 memory
  state, so the first simulated interval of a card is its real one.
