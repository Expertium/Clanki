- RWKV-Curve now appears on the model-quality graphs. Its prediction of a past
  review is the forgetting curve it had stored at that card's previous answered
  review, read at the time since then, and the RWKV replay records it for every
  review as it goes. Until now the graphs said RWKV-Curve "cannot compute its
  prediction for a past review", which was never true: the model computed the
  number during every warm-up and Clanki threw it away.
- An algorithm whose predictions have not been recorded yet now says exactly
  that, and says what records them, instead of claiming the algorithm is
  incapable. If the recording only reaches part of your history, the graph says
  from when it reaches and how many earlier reviews it does not cover.
- Rebuilding the RWKV review history records RWKV-Curve's predictions for the
  whole history in the same pass that records RWKV-Instant's, so it costs no
  extra walk over your reviews.
