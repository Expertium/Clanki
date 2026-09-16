- Total Knowledge graph: Simple mode now draws only the Known line, and says in
  plain words what it is — Clanki's best estimate of how many cards you knew at
  each point in your review history — without the word "retrievability".
  Advanced mode keeps the grey Reviewed line behind a checkbox (on by default),
  and names your scheduling algorithm and the fact that Reviewed is an upper
  bound: it counts every card you have ever rated, as if you never forgot one.
- Total Knowledge graph: switching the Stats page between Simple and Advanced no
  longer computes the graph again; a running RWKV replay keeps the days it has
  reached. The tooltip now rounds Known to a whole number of cards.
