- RWKV-Curve and RWKV-Instant answer with FSRS states also in a new
  collection whose FSRS switch is still off. Before, such a collection showed
  and stored SM-2 intervals under RWKV-Curve until deck options were saved.
- When building RWKV-Curve's answer states fails, the answer buttons keep
  waiting. Before, they showed FSRS-7's intervals, and the answer stored them
  with RWKV-Curve's stability.
- The RWKV-Curve reschedule no longer writes RWKV-Curve's stability into
  FSRS-7's fast stability of a card that had none.
- The RWKV-Curve reschedule turns intervals into days as the FSRS-7
  reschedule does: rounded to the nearest day (not up), capped at the
  preset's maximum interval, and spread by fuzz, the load balancer and Easy
  Days, so cards with the same interval no longer all land on the same day.
- Under RWKV-Curve, "Leech only if young" uses RWKV-Curve's stability for
  Again, not FSRS-7's.
