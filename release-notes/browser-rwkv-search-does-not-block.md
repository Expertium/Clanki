- A Browser search that asks for RWKV values (`prop:rwkv:r…`,
  `prop:rwkv-curve:r…`) no longer freezes the window while RWKV loads its
  state. There is no progress window over the Browser: the table keeps its
  rows, the editor keeps its note, and the title says that Clanki is
  calculating RWKV values until the new rows arrive. Before, the search could
  hold the collection for two minutes behind a progress window, so the first
  Browser open showed nothing for that long.
