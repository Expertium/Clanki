- The pass that records RWKV's per-review data now uses a model of its own,
  so it no longer takes the one the reviewer answers with. It loads a second
  copy of the same model, replays your history into that one and gives it
  back when it is done. While it runs you can answer cards as usual: the
  reviewer reads a state the pass never touches, instead of waiting for the
  pass or making the pass step aside. The second copy costs about 11 MB, and
  the pass no longer has to keep a copy of the reviewer's state to put back
  afterwards.
- Because of that, "Getting this card ready..." no longer appears while the
  pass runs. Reviewing a card still stops the pass, as a safety net, but
  there is now nothing for it to wait for.
