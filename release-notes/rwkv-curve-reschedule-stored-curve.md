- "Reschedule cards when desired retention changes" under RWKV-Curve takes each
  card's new interval and S90 from the forgetting curve RWKV stored at the
  card's last review, the same curve card info, the Browser, filtered decks and
  Stats show. Before, it used a curve that the model was never trained to give,
  so a rescheduled card could get a date that no other RWKV-Curve screen agreed
  with.
