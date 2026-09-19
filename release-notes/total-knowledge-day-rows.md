- The Total Knowledge graph under RWKV-Instant is much faster on a large
  collection. It used to build the whole card set again for every day of the
  collection's history; it now keeps the rows and only tells the model which
  day to score. The numbers it draws are unchanged.
