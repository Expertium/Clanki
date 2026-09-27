- Clanki writes its log to `clanki.log` in the `logs` folder of the data
  folder (next to the add-on logs), also when it runs without a console. The
  RWKV and FSRS-7 background passes log their steps there in detail, so a
  wait or an error can be looked at afterwards. The file is kept to five
  files of 10 MB.
