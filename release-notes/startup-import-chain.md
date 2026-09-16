- Clanki starts faster. The window used to wait while Python loaded `bs4`,
  `requests` and `markdown`, which are only needed to paste HTML, fetch a
  picture by URL, download an add-on or show one error message. They now load
  the first time they are used, which takes about 80 ms off every start.
