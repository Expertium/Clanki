- After you answer a card, the study queue is kept and only updated, as in
  Anki, instead of being built again. The next card shows sooner: the
  window no longer freezes for about 60 ms (RWKV-Curve) or 430 ms (FSRS-7)
  after each answer on a deck with 20,000 due cards. The retrievability
  orders now keep the order the queue was built with during a session
  (until another action or a new day rebuilds it).
