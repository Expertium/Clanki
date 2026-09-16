- Under RWKV the Stats page draws its Probability of recall (Retrievability)
  graph about three times faster: the graph shows RWKV's score of each card and
  computes nothing from the card itself, so Clanki now reads only each card's id
  and its note's id instead of every card in full (a collection of 159,000
  cards, deck "Main": 1,155 to 394 ms). The graph is the same.
