- Windows: starting or stopping a card's audio no longer holds up the
  window. Clanki waited for the audio player's reply for up to 0.1 s (50 ms
  on average) on every command; the reply now arrives in about a
  millisecond, so the answer side of a card with audio shows about 45 ms
  sooner.
