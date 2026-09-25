- A sync from a device that turned FSRS off (Anki, AnkiDroid) no longer
  undoes an algorithm you chose later in Clanki. Clanki keeps a short history
  of algorithm changes (it syncs), and compares the time of your last choice
  with the other device's change: the newer one wins. When a sync changes the
  algorithm, a short notice says so ("A sync from another device turned FSRS
  off, so Clanki now uses RWKV-Curve.").
