- Switching the Stats page from Simple to Advanced is faster. While the page
  shows Simple mode, Clanki computes the Advanced graphs in the background, so
  the click no longer waits for the backend to read every searched card (a
  collection of 159,000 cards, deck "Main", 12 months: the whole switch 10.9 to
  9.1 s under RWKV-Curve, 5.3 to 2.8 s under FSRS-7). The page shows the same
  graphs.
