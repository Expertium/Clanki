- The pass that stores FSRS-7's prediction of every past review — the one that
  fills the data behind the Stats page's model-quality graphs, and the one the
  FSRS optimizer's own evaluation runs five more times — now replays cards in
  parallel and evaluates the forgetting curve without building a tensor for each
  review. On a 159,000-card collection it went from 97 s to 25 s for 527,365
  reviews, and the whole evaluation with its validation folds from 346 s to 76 s.
  Every stored value is unchanged.
