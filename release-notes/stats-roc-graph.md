- The Stats page in Advanced mode has a new AUC-ROC graph. It draws one curve
  per scheduling algorithm — FSRS-7, RWKV-Curve and RWKV-Instant — from the
  reviews of the search in the chosen period, and gives each one its area under
  the curve, so you can see which algorithm separates the reviews you remembered
  from the ones you forgot. The drawing area is square and the dashed diagonal
  is random chance. An algorithm that cannot be computed is left out, with a
  line that names it and says why; it is never replaced by another algorithm's
  curve.
- RWKV's predictions come from a background replay of your review history, so
  the page stays usable while the graph reads "Calculating…". The result is kept
  for the session and survives a Simple/Advanced switch.
