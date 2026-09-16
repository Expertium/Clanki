# Stats graphs: clearer colours, and axes that step by 0.1

The Card Retrievability graph drew its RWKV bars in amber. They are blue
now, and the FSRS-7 series stays green.

The Calibration graph drew its count bars in grey, which read as a graph
that was turned off. They are blue now. The dashed line of perfect
calibration stays grey, and so does the AUC-ROC graph's line of random
chance: only the two reference lines are grey.

The AUC-ROC and Calibration graphs step both axes by 0.1, not by 0.2.
