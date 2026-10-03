- Adding a deck, a filtered deck or a Custom Study session no longer makes
  RWKV-Curve read your whole review history again later. Before, the next
  settings change (a Preferences save, for example) made the next card wait
  about two minutes on a large collection, and the next start of Clanki read
  the history again before the first card. Deleting a deck that has no
  reviewed cards now keeps the loaded history too; before, the next card
  waited the same two minutes.
