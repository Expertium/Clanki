- The RWKV statistics pass now runs when you open Stats or Card Info, and
  not before. Starting Clanki, clicking a deck and reviewing no longer share
  the machine with a pass that replays your whole review history, and a
  collection you never take statistics on never pays for one.
- The pass carries on where it stopped. Closing the Stats page, or Clanki
  itself, keeps the work it has already done; the next time you open Stats
  it goes on from there instead of starting again.
- Every step of the pass is short. Reading the history used to take one step
  of about twelve seconds on a large collection, during which Stats would
  not open and the deck list was slow. It is now cut into steps of about a
  third of a second, with a rest between them.
- Clanki now counts the recorded rows to decide whether the pass has run.
  A single card answered in the reviewer used to be enough to make the pass
  look finished, which left the model-quality graphs and Card Info's
  forgetting curve empty for ever on a collection that had never completed
  one. Those two screens fill in by themselves now.
