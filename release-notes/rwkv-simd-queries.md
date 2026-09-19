- RWKV does less work per prediction on Windows and Linux: retrievability
  queries now pass through the model in blocks of rows (about 2.5 times less
  CPU time per queried card), and on CPUs with AVX2 (most x86 CPUs since
  2013) the replay's projections run about 1.6 times faster. Predictions
  change only within float rounding (at most 0.000001 on a real collection).
