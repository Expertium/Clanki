- Clicking Study while the deck overview was still updating its counts
  sometimes left the overview on the screen: the first card was loaded behind
  it, and Space and "Study Now" did nothing. This happened most often right
  after start-up, when the RWKV state finished loading. The late counts are
  now dropped, and the first card shows.
