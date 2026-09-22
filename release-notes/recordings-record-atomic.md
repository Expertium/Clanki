- The RWKV recording pass no longer loses its place because of its own
  bookkeeping. It writes where it has got to in a small file, and a screen
  reads that file while it is written; the write emptied the file first, so a
  read could land on nothing and the next pass would start the whole history
  over. The file is now replaced whole.
