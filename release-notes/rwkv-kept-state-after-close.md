- If you delete or move cards with reviews and close Clanki soon after, the
  next start no longer makes the first card wait. Before, RWKV's background
  rebuild after such a change stopped at the close, and the next start built
  RWKV's state again from your whole review history: the first card showed
  "Getting this card ready…" for about three minutes. Now the next start
  uses the state saved before the change, as the session before did, and
  rebuilds it exactly in the background.
