- The model-quality graphs on the Stats page (AUC-ROC and Calibration) now draw
  every algorithm on all the reviews that algorithm can score. Until now they
  kept only the reviews **all** the algorithms could score, so a single
  algorithm with a handful of stored predictions could empty the graph: on a
  collection where RWKV had predictions for 9154 of a deck's 9155 reviews and
  FSRS-7 had four, three reviews survived, all three had been answered Again,
  and both curves disappeared with "these cards have no review it predicts".
- The notes under the graph now say how many reviews were scored at all, how
  many each drawn algorithm scored, how many every algorithm scored, and how
  many no algorithm scored. Two scores can be compared directly only over the
  reviews the algorithms share, so the graph names that number instead of
  throwing the other reviews away.
- No algorithm is ever filled in from another algorithm's values. A review one
  algorithm cannot score is simply absent from that algorithm's curve.
