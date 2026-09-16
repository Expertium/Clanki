- The reviewer no longer waits for ever at "Waiting for RWKV-Curve…". While
  the answer buttons wait, Clanki now loads RWKV-Curve's state in the
  background, so a state that went cold during the session (after a bury, a
  sync, an undo, an edit or a deck-options save) comes back without an answer.
  Before, only answering a card loaded the state again, and the wait blocked
  the answer.
- When RWKV-Curve has no interval for the card, the button area says so at
  once. When RWKV-Curve does not give the intervals in 60 seconds, the button
  area says what happened, what to try, and offers a "Try again" button on the
  same card. FSRS-7 intervals still never stand in for RWKV-Curve's.
