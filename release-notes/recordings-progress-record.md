The RWKV recording pass now leaves a record of what it did, in
`recordings-progress.json` beside the state cache. It says when the pass last
did something, whether it is running, finished or stepped aside for a review,
and how far it got. Nothing shows it; it is there so a question like "why has
this graph no data" can be answered from a file instead of guessed.
