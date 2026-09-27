- Fixed a rare crash: a delayed action (a dialog repositioning itself, or a
  "cards updated" tooltip after suspending, moving, tagging or flagging cards)
  no longer errors out if the window it belonged to had already been closed.
  The action still finishes; a tooltip that lost its window shows over the
  main window instead.
