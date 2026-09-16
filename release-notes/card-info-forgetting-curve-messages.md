- Card Info's Forgetting Curve box no longer says "NO DATA". When it has no
  curve, it says why in one short line: the card has no answer yet, the card
  was reset, the card needs a review on a later day, or RWKV-Curve has no curve
  for it yet.
- While RWKV is still loading its state, the box says "Calculating…" and the
  page asks again on its own, so the curve appears without closing and
  reopening Card Info.
- Under RWKV-Curve the curve now also draws for a card that was reset. RWKV's
  stored curve does not need an FSRS-7 memory state.
