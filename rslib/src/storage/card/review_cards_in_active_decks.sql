SELECT id,
  nid,
  due,
  cast(ivl AS integer),
  cast(mod AS integer),
  did,
  odid,
  reps,
  queue
FROM cards
WHERE did IN (
    SELECT id
    FROM active_decks
  )
  AND queue IN (2, 3)