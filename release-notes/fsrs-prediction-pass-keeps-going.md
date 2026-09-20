- The pass that keeps FSRS-7's per-review predictions ready now goes on
  while you study. It used to wait for ten seconds of quiet before every
  preset, and every key press started that wait again, so on a large
  collection it made no progress at all during a review session: measured,
  0 of 11 presets and 0 rows in five minutes of use, for a job of 25
  seconds. It now rests between two presets for a short, bounded time
  instead, and wrote all 966,822 rows in 52 seconds of the same use. It
  still waits for a pause before it begins, so it never lands on your first
  clicks after start-up.
- A pass caught in that wait used to stay alive for up to ten seconds after
  the profile closed under it. It now stops within a quarter of a second.
