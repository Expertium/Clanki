- The first click on a deck is no longer slow. Clanki loads its RWKV state when
  you open a profile, and that work used to hold the one worker that every
  other collection job waits for, so an early click waited its turn. It now
  runs beside them. On a 656,000-review collection the first click's counts
  arrived in 51 ms instead of 1289 ms, and the page appeared in 1.0 s instead
  of 2.3 s.
