- Saving Preferences no longer makes the next card wait. After a save that
  changed any setting, RWKV-Curve threw its loaded review history away and
  read it again, and the answer buttons showed "Getting this card ready…" for
  1.5 to 5 seconds on a large collection. It now keeps the loaded history.
- RWKV-Curve now notices every change of the day's start: another "Next day
  starts at", a daylight-saving change, a new timezone on your computer. Such
  a change moves reviews from one day to the next, so RWKV-Curve reads the
  review history again. It does that in the background and no card waits:
  until the new reading is ready (about two minutes on a large collection),
  the intervals come from the old one and are off by under 1 % on average
  and by a few percent at most. Before, a changed "Next day starts at" made
  the next card wait for the whole reading (two to three minutes), and a
  changed timezone went unnoticed for one session.
