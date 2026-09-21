- RWKV-Instant no longer shows the settings that shape an interval, and no
  longer obeys them. Learning steps, Relearning steps, Maximum interval,
  Minimum interval and Maximum number of same-day reviews are hidden while
  RWKV-Instant schedules a preset, and a card it answers never enters the
  learning or relearning queue. RWKV-Instant decides when a card comes back
  from the card's own score, so an interval setting had nothing to act on.
  The values stay in the preset: a preset that goes back to FSRS-7 or
  RWKV-Curve has its steps and its intervals again.
