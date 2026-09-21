- The check that says whether the RWKV state cache is still current is five
  times faster. It answers one question -- has the review history changed since
  the cache was written? -- and it used to answer it with a query that walked
  the review log five times, once per card through the card index, and then
  loaded every card of the history to find which preset each one uses. It now
  reads the review log once, in one sequential pass, works out each card's
  start row while it reads, and takes the preset from the card's deck, which
  needs no card at all unless an add-on rule moves a card to another preset. On
  a 656,000-review collection the check went from 3.54 seconds to 0.69 seconds.
  It returns exactly the same answer.
