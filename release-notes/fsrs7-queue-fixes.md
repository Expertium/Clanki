- FSRS-7: the retrievability and relative-overdueness review orders keep
  learn-ahead. A (re)learning card due within the learn-ahead limit is now
  shown and counted, as in the other orders, and due learning cards come by
  due time instead of waiting behind the due reviews.
- FSRS-7: for a card with no recorded last review time, the Browser,
  searches, the study queue and filtered decks now estimate the time since
  its last review with one rule, so they show and sort by the same
  retrievability. Before, searches and the queue counted a learning card's
  interval in days as extra seconds, and took a card whose interval began
  before the collection was created as just reviewed.
- FSRS-7 with add-on preset overlays: the retrievability review orders and
  the retrievability filtered-deck orders build faster, since each overlay
  rule is now one search for all cards instead of one search per card.
