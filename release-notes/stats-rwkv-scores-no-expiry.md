- The Stats page keeps RWKV's retrievability scores until they really change.
  There is no time limit on them any more: the scores of one calculation
  serve every later request for the same search on the same day, so switching
  between Simple and Advanced mode, or between the last 12 months and all
  history, never starts the calculation again. Answering a card, changing
  cards or queues, a new day and another search still start a fresh
  calculation.
