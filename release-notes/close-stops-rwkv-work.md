- Closing Clanki or switching profiles while RWKV work runs in the background
  no longer logs dozens of "CollectionNotOpen" errors. The close first stops
  that work cleanly; the next session goes on from where it stopped. No deck
  list read runs after the collection has closed.
