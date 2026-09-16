- The Stats Retrievability graph is much faster under RWKV-Curve. The graph
  needs one number per card, and RWKV now computes that number on its own
  instead of running the four simulated-answer passes and the interval search
  that the graph never uses. The numbers the graph draws do not change.
