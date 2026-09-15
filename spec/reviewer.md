# Reviewer

## review.answer-buttons

Given a review card, the answer buttons carry a coloured border by default: red
for Again, green for Hard, Good and Easy (upstream PR 4371). Given the
collection flag `twoButtonMode` (default on), only Again and Good are shown
for a card that would otherwise get four buttons; Good keeps ease 3, so
scheduling is exactly the four-button Good. Answer keys then follow the two
buttons on screen: key 1 is Again, key 3 is Good, and the Hard and Easy keys
(2 and 4) do nothing.
Both options are collection preferences (Preferences > Review: "Show colored
border on answer buttons", "Show only Again and Good answer buttons") and
apply in Simple and Advanced UI mode alike. The `showColoredButtons` flag
only changes markup; cards with two or three scheduler buttons (learning
steps) are unchanged.

**Why:** Andrew, 2026-09-14: the out-of-the-box reviewer should look like
this in every UI mode. Andrew, 2026-09-15: with only two buttons, the Hard
and Easy shortcuts must be ignored (key 2 used to answer Good).

**Pinned by:** `test_two_button_mode_offers_again_and_good`,
`test_two_button_mode_maps_answer_keys` (`qt/tests/test_reviewer.py`),
`answer_button_options_default_to_on` (`rslib/src/config/bool.rs`).

## review.timer-keeps-running

Given the on-screen timer shown on the Study screen (the preset's "Show
on-screen timer"), it keeps counting after the answer is shown, until the
card is answered. A preset's stored "Stop on-screen timer on answer"
(`stopTimerOnAnswer`) is kept as it is, for other clients, but not used, and
deck options no longer show it in either mode. The time recorded for an
answer does not depend on the on-screen timer.

**Why:** Andrew, 2026-09-15: remove the setting and treat it as off in both
modes, so Simple mode's single timer switch stands for one setting and needs
no "Partly on" caption.

**Pinned by:** `test_on_screen_timer_keeps_running_when_the_answer_shows`
(`qt/tests/test_reviewer.py`).
