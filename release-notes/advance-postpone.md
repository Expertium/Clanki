- Advance and Postpone, from the FSRS Helper add-on, are built in (Advanced
  mode): "Advance Cards..." and "Postpone Cards..." in the deck menu (the
  gear next to a deck) and in the Browser's Cards menu. Advance makes review
  cards that are not due yet due today, the ones closest to their target
  first; Postpone moves due cards a few days later, the least overdue first,
  through the fuzz range and the load balancer. A dialog asks how many cards
  to move, says how many are relatively safe to move, and shows the effect
  on their retrievability. They work with FSRS-7 and with RWKV-Curve (each
  card's own RWKV-Curve curve), not with RWKV-Instant. A move is one undo
  step and writes nothing to the review history.
