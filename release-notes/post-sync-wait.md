- The wait after a sync is now as short as the sync was. Clanki used to read
  every review in the collection again to add the few the sync brought, behind
  a "Getting Ready" window. On a 656,000-review collection that read took about
  16 seconds of pure work, and Andrew measured the window at about 30 seconds.
  Clanki now reads only the new reviews and the cards they belong to: the same
  read of 18 new reviews takes 70 milliseconds.
