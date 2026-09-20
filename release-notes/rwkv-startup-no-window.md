- Opening a profile no longer shows a "Starting" window and no longer locks
  Clanki up while the RWKV state is restored or built. The main window is
  there straight away: the deck list, the menus and every dialog work while
  that finishes in the background. Screens that need an RWKV number wait for
  it quietly and fill in by themselves; none of them shows a number of the
  other algorithm instead.
- The one wait that stays is the one-time conversion of a cache written by an
  older Clanki, which still says so in its own window.
