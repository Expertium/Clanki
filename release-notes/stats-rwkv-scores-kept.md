- The Stats page no longer asks RWKV to score every card again when only the
  page changes. Switching between Simple and Advanced mode, or between the
  last 12 months and all history, keeps the scores of the first calculation
  for up to 10 minutes. The Retrievability graph then shows RWKV's R as of
  that first calculation. Answering a card, changing cards or queues, a new
  day and another search all start a fresh calculation, as before.
