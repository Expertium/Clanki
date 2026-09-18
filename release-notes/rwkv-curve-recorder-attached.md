- The RWKV-Curve series really is recorded now. The last change gave the replay
  the number and a recorder to put it in, but never connected the two, so
  "Recomputing RWKV calibration data" ran for two and a half minutes over
  656,412 reviews and wrote no RWKV-Curve row, and the Stats graphs stayed
  empty.
- A recompute that cannot record RWKV-Curve's predictions now stops before it
  starts and tells you so, instead of replaying your whole history and
  recording nothing.
