- Saving Preferences no longer makes the next card wait. After a save that
  changed any setting, RWKV-Curve threw its loaded review history away and
  read it again, and the answer buttons showed "Getting this card ready…" for
  1.5 to 5 seconds on a large collection. It now keeps the loaded history
  unless you changed "Next day starts at". That setting moves reviews from
  one day to the next, so RWKV-Curve still reads the history again after it.
- RWKV-Curve now notices a change of the day's start that it could miss
  before: a new timezone on your computer, or a "Next day starts at" that came
  from a sync. At the next start it checks its saved history and reads it
  again when reviews moved to another day. Before, it kept the saved history
  for one more session.
