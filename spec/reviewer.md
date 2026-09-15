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
