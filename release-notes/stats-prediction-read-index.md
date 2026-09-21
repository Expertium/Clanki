The Stats model-quality graphs read their per-review numbers faster. The
cache beside the collection now holds an index that covers the whole
read, so it no longer looks up a million rows one at a time. On a
collection with 656k reviews the storage cost of the FSRS-7 read drops
from about 1.9 s to about 0.13 s, and RWKV-Curve's from about 0.66 s to
about 0.37 s.

A collection that already has a cache builds the index once, which takes
about 4.5 seconds on a collection that size and happens on one start
only. The cache file grows by about 98 MB.
