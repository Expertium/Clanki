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

## review.four-button-notice

Given the user turns "Show only Again and Good answer buttons" off in
Preferences > Review, and so turns the four answer buttons on, Clanki shows a
notice once the Preferences window has closed. The notice explains the four
buttons the way the manual does -- what Again, Hard, Good and Easy mean, each
with its keyboard shortcut (1, 2, 3/Space/Enter, 4) -- and asks the user to
accept it with "I understand that Hard is a passing grade".

The notice's second button, "I want to use Hard as fail", starts straight
above the button that accepts the notice. Whenever the cursor comes within 60
pixels of it, it flies to a far corner of its strip over 220 ms instead of
jumping there, and a press that does land does nothing: no answer is changed,
no setting is written and the notice stays open. The notice cannot be
dismissed at all -- there is no close button in its title bar, and Esc and
Alt+F4 do nothing -- so accepting it is the only way out. The only way back to
two buttons is the checkbox.

The notice appears the first time and never again: a flag in the profile
(`fourButtonNoticeShown`) is written when it is shown. In a developer's build
-- one started with `ANKIDEV` set, or by the source launcher `tools/run.py`,
which marks its run -- it is shown on every switch instead, which is how it is
tested by hand. The notice changes no preference of its own.

**Why:** Andrew, 2026-11-15: "When the user switches to 4 button mode for the
first time, display a pop-up with the following text from the manual" -- the
manual's four button descriptions and their shortcuts -- "And the the pop-up
should have 2 buttons at the bottom: 1) I understand that Hard is a passing
grade 2) I want to use Hard as 'fail'. Except here's the fun part: the second
button flies away from the cursor. When the cursor is close enough to it, it
moves to a different spot on the pop-up, so the user can never catch it (and
even if he does, it would do nothing)". Andrew, 2026-11-15: "in the PR this
should only work once - the first time the user enables the 4-button mode. In
the local build the pop-up should appear every time I switch, for testing
purposes." Andrew, 2026-11-19: "1) The 'I want to use Hard as \"fail\"' text
button be straight above the other button at first 2) Make it more animated.
Right now the button teleports instead of smoothly flying 3) Make sure the
pop-up window cannot be closed by clicking the cross icon".

**Pinned by:** `test_four_button_notice.py` (the manual's words,
`test_the_notice_is_shown_once_and_remembered`,
`test_an_ankidev_build_shows_the_notice_every_time`,
`test_a_run_started_by_the_source_launcher_shows_the_notice_every_time`,
`test_the_source_launcher_marks_its_run`,
`test_the_button_hides_farther_than_the_cursor_can_reach`,
`test_the_button_starts_straight_above_the_understand_button`,
`test_the_button_flies_to_its_new_spot_instead_of_jumping`,
`test_the_notice_cannot_be_closed_with_the_cross_or_escape`,
`test_pressing_the_fleeing_button_does_nothing`);
`test_preferences.py`
(`test_turning_four_button_mode_on_asks_for_the_notice`,
`test_the_pending_notice_is_shown_once_the_dialog_has_closed`).

## review.card-style-with-content

Given a card on the Study screen (or in the card preview), its body classes
(`card`, `cardN`, the night-mode classes) are set in the same step as its
content goes into the page, before the card's own scripts load. No frame
shows a card's content without its note type's styling: its background,
its alignment and its text colours come with the first frame of the card,
also while a script the card loads (`<script src="...">`) is still loading.
Hooks that add-ons put in `onUpdateHook` still run after the content and
its scripts, as before, and the classes are set again there.

**Why:** Andrew, 2026-09-26, from a screen recording taken frame by frame:
"the card's front is not fully loaded on the first frame". His Japanese
note type loads two scripts from the media folder and styles `.card` (dark
background, centred content). The classes were set only after the scripts
had loaded, and Chromium painted the card in between: a light background,
light grey text on it, the picture on the left; the next frame had the
card's dark background and the centred picture.

**Pinned by:** `ts/reviewer/card_style.test.ts`.

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

## review.first-card-one-frame

Given Study Now (or any start of the Study screen that loads the reviewer
page), Clanki loads the reviewer page and its bottom bar hidden (spec
ui.screen-one-frame) and shows them together once the first card is in the
page: its content, its styling and its scripts done. The bottom bar is shown
with the card's Show Answer button and the remaining counts already in it.
Until then the screen keeps showing the deck's overview. The reviewer's wait
for the frame that shows the card (the paint wait before Show Answer and the
answer keys work) starts only once the page is shown, so it still waits for
a frame the user sees. A first card not in within 0.5 s is shown as it
fills in, as before. Later cards are drawn as before.

**Why:** Andrew, 2026-09-26, from a screen recording taken frame by frame:
"when I click "Study Now" for one frame "Options", "Custom Study" and
"Description" disappear and are replaced with "Edit" and "More"." The
bottom bar's page was ready before the main view's first card, and its Show
Answer button came a frame or more after the card. Measured offscreen on a
copy of Andrew's collection: the bottom bar changed twice (Edit and More
alone, then Show Answer) and the main view up to three times; now each
changes once, within one frame of each other.

**Pinned by:** `qt/tests/test_reviewer_first_frame.py`
(`test_the_first_card_gets_its_answer_button_before_both_pages_show`,
`test_a_later_card_draws_its_answer_button_when_presented`,
`test_a_stale_first_card_only_shows_the_pages`,
`test_study_now_draws_the_page_and_the_bottom_bar_held`);
`ts/reviewer/held_page.test.ts`.
