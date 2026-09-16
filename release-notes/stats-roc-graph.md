- The Stats page in Advanced mode has a new AUC-ROC graph. It draws one curve
  per scheduling algorithm — FSRS-7, RWKV-Curve and RWKV-Instant — from the
  reviews of the search in the chosen period, and gives each one its area under
  the curve, so you can see which algorithm separates the reviews you remembered
  from the ones you forgot. The drawing area is square and the dashed diagonal
  is random chance.
- The graph reads the predictions each algorithm stored while it ran, so it
  opens at once and starts no computation. It uses only predictions made before
  the algorithm had seen the review, it scores every algorithm on the same
  reviews, and it says how many reviews it used, how many it left out and how
  fresh the predictions are. An algorithm it cannot score is left out with a
  line that names it and says why; it is never replaced by another algorithm's
  curve.
