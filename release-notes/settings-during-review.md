- Changing a setting while you review no longer stops the answer buttons.
  Clanki treated every settings change as a reason to throw away RWKV-Curve's
  loaded state and build it again, which on a large collection took about ten
  seconds with "Getting this card ready…" over the buttons. It now keeps that
  state whenever the change cannot affect it, and a switch such as the
  two-button mode cannot. A change that does affect it, such as moving a deck
  to another preset, still rebuilds.
