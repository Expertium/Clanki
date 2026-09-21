- The check that says whether the RWKV state cache is still current is four
  times faster. It answers one question -- has the review history changed since
  the cache was written? -- and it used to answer it with a query that walked
  the review log five times, once per card through the card index. It now reads
  the review log once, in one sequential pass, and works out each card's start
  row while it reads. On a 656,000-review collection the check went from 3.54
  seconds to 0.85 seconds. It returns exactly the same answer.
