- Stats (Advanced mode, RWKV): the graphs no longer wait for RWKV to score
  every card of the deck for the Retrievability graph, which can take
  minutes on a big collection. The other graphs are drawn at once, and the
  Retrievability graph says "Calculating…" until RWKV's values arrive.
