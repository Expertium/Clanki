- Delete Note in the reviewer no longer opens a "Processing..." window, and
  the next card's answer buttons are ready at once. Before, deleting a note
  with reviews threw RWKV's state away: every later card said "Getting this
  card ready…" for a minute and then gave up, until Clanki was restarted.
  RWKV now keeps its state and rebuilds it exactly in the background, while
  you review; the rebuilt state is saved, so the next start is quick too.
