- After you delete or move cards with reviews, a sync no longer makes
  RWKV-Curve read your whole review history again. A full upload right after
  such a change shows no "review history after sync" window any more (it
  showed one for about 100 s on a large collection), and the next card is
  ready at once. A normal sync that brings changes from another device shows
  that window for a shorter time (about 30 s instead of 100 s). If you close
  Clanki right after the upload, the next start no longer rebuilds the whole
  history while the first card waits (about 15 s instead of 3 minutes).
