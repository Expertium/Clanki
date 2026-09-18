- FSRS-7's predictions of your past reviews really are stored now. The
  automatic pass died on its own first line every time it ran, because it read
  a field off a list of preset ids that has never had one, so a collection of
  910,762 rated reviews held 84 predictions and the model comparison graphs
  had no FSRS-7 series. The pass now stores every preset; 11 stale presets take
  about 20 seconds in total.
- A pass that fails now tells you once, instead of leaving an empty FSRS-7
  series that looks the same as one still being computed. A failed pass also
  does not count as that day's work, so it runs again.
