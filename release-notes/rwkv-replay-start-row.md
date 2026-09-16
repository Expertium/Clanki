- RWKV now gives a card whose review history has no Learning row the same
  first-review treatment as any other card. Such a history comes from an
  import, from another application or from an old scheduler. The card starts at
  its first rated review after its last Forget, or at its first rated review
  when it was never forgotten. Before this, the replay read that first review
  as a mid-history review with no state behind it, and it kept the reviews from
  before a Forget. Set Due Date does not cut the history; only the last Forget
  does.
