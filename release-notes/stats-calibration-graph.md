- The Stats page in Advanced mode has a new Calibration graph. Pick one
  algorithm from its menu and it shows, for each group of reviews with a similar
  predicted probability of recall, how many you really remembered: the dashed
  diagonal is a perfect algorithm, a point above it means the algorithm
  predicted too little and a point below it too much. Behind the line are the
  reviews of each group, and through each point is the range its actual recall
  lies in with 95% confidence. Three tiles give the average predicted
  probability, the actual recall and the number of reviews.
- The menu keeps its choice while the page reloads, and picking another
  algorithm computes nothing again. An algorithm without usable predictions is
  in the menu but cannot be chosen; it is never replaced by another one.
- Two or more algorithms drawn together are compared only on the reviews they
  can all score, and the graph says so; one algorithm on its own keeps all of
  its own reviews.
