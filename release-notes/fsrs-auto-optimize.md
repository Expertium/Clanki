- FSRS-7 now optimizes its parameters by itself. Each preset has "Optimize
  every N days" (7 by default, 0 = never) in the FSRS advanced section of
  deck options. Clanki runs the optimization in the background when you are
  not using it, under RWKV too, so the Stats comparison with FSRS-7 stays
  current; under RWKV it never changes a due date. The "Time to optimize"
  reminder shows only for presets set to 0.
