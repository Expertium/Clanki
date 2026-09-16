# AnkiConnect (built in)

Clanki has the AnkiConnect add-on (AnkiWeb 2055492159, GPLv3 or later)
built in, together with the additions of its JSchoreels fork, "AnkiConnect
Extended" (https://github.com/JSchoreels/anki-connect). Its HTTP API is a
hard compatibility boundary (plan item 4): existing clients (Yomitan,
Yomichan, Obsidian plugins, scripts) must keep working unchanged.

## ankiconnect.http-protocol

Given AnkiConnect on, Clanki listens on the configured address and port
(127.0.0.1:8765 by default) and answers each connection the way the add-on
did (its web.py): one request per connection, read up to the
`Content-Length` given in its headers, any method and path; the reply
carries `Content-Type: application/json`, `Access-Control-Allow-Origin` and
`Access-Control-Allow-Headers: *` and the connection is closed after it.

- A request without an `Origin` header (a program, not a web page) is
  allowed. A request with one is allowed when the origin is in the allowed
  origins, or when the list holds `*`, or when it holds
  `http://localhost` and the origin is `http://127.0.0.1`,
  `https://127.0.0.1`, `http://127.0.0.1:<port>` or a `chrome-extension://`,
  `moz-extension://` or `safari-web-extension://` origin. The deprecated
  single origin (`webCorsOrigin`, else the `ANKICONNECT_CORS_ORIGIN`
  environment variable) counts as one more allowed origin.
- `OPTIONS` gets an empty 200 reply; with
  `Access-Control-Request-Private-Network: true` it also carries
  `Access-Control-Allow-Private-Network: true`.
- A request from an origin that is not allowed gets
  `HTTP/1.1 403 Forbidden` with an empty body, unless its action is
  `requestPermission`.
- An allowed request with an empty body gets
  `{"apiVersion": "AnkiConnect v.6"}`; a body that is not JSON or does not
  match the request schema (an object with a non-empty string `action`, an
  integer `version`, an object `params`) gets `{"result": null, "error":
  <message>}`.
- Every other request runs its action and gets
  `{"result": <result>, "error": null}` for `version` 5 or later, the bare
  result for `version` 4 or earlier or none, and `{"result": null,
  "error": <message>}` when the action fails (any version). An unknown
  action fails with "unsupported action"; a wrong parameter with Python's
  message, e.g. "AnkiConnect.findNotes() got an unexpected keyword
  argument 'x'".
- With an API key set, every action except `requestPermission` fails with
  "valid api key must be provided" unless the request's `key` equals it;
  without one, a request carrying any `key` fails the same way, as in the
  add-on. `multi` checks each of its actions (each needs its own `key` and
  gets its own `version`).
- `requestPermission` from an allowed origin answers `{"permission":
  "granted", "requireApikey": <key set>, "version": 6}`; from an origin in
  the ignored list, `{"permission": "denied"}`; from any other origin it
  asks the user ("A website wants to access Clanki"): Yes adds the origin
  to the allowed origins and grants; No denies, and with "Ignore further
  requests" ticked adds the origin to the ignored list.

Requests are answered one at a time, in the order they arrive, as in the
add-on; the connection's own thread reads and writes the socket.

**Why:** plan item 4 (the add-on's API is a compatibility boundary);
Andrew, 2026-09-15: integrate AnkiConnect natively.

**Pinned by:** `qt/tests/test_ankiconnect.py` (`test_version_over_http`,
`test_api_version_4_gives_the_bare_result`,
`test_empty_body_gives_the_api_version`, `test_invalid_json_gives_an_error`,
`test_schema_is_checked`, `test_unsupported_action`,
`test_wrong_parameter_gives_pythons_message`,
`test_api_key_is_required_when_set`,
`test_api_key_without_one_set_a_key_is_refused`, `test_cors_origins`,
`test_cors_star_and_listed_origins`, `test_cors_deprecated_single_origin`,
`test_options_preflight`,
`test_forbidden_origin_gets_no_body_and_runs_nothing`,
`test_request_permission_granted_for_allowed_origins`,
`test_request_permission_asks_and_yes_allows_the_origin`,
`test_request_permission_no_with_ignore_denies_from_then_on`,
`test_multi_runs_each_action`,
`test_requests_run_one_at_a_time_off_the_main_thread`,
`test_gui_actions_go_to_the_main_thread`, `test_large_body_is_read_whole`).

## ankiconnect.actions

Given a request for any of the add-on's actions or its fork's, Clanki runs
it with the add-on's parameters and gives the add-on's result and error
text, with these differences:

- The fork's additions are included: `cardsInfo` takes `fields` (`prop:r`,
  `prop:s`, `prop:d`), `noteFields` and `retrieved_info_mode` (`ALL`,
  `COMPACT`, `FIELDS_ONLY`); `findCards` takes `fields` and `noteFields`
  and then answers as `cardsDetails`; new actions `cardsDetails`,
  `gradeNow`, `repositionNewCards`, `guiAddNoteSetData`, `guiPlayAudio`;
  media given to `addNote` without `fields` is stored but not added to a
  field; `exportPackage` and `importPackage` use the current package
  format calls (import with scheduling and presets).
- `cardsInfo` in `ALL` mode gives the add-on's keys in the add-on's order.
- `reloadCollection` does nothing but fail without a collection (the
  add-on's call has done nothing since Anki 2.1.50).
- After an action that changes the collection, the open screens refresh
  for what it changed (as after an edit in Clanki itself), once for a
  burst of requests, 0.1 s after the last and never while a request runs;
  the add-on reset every screen before each such action. The changes
  Clanki's own screens make without rebuilding the RWKV state (adding,
  editing, tagging and deleting notes, suspending, forgetting, Set Due
  Date, repositioning new cards, creating a deck, answering) keep it here
  too; any other change (`insertReviews`, note type and preset changes,
  `changeDeck`, `relearnCards`, an import, and so on) is refreshed for on
  its own, right after it, so RWKV sees it. `replaceTags` and
  `replaceTagsInAllNotes` show no progress window.
- `sync` runs the add-on's normal sync as a background task (it still fails
  when a full sync is needed), tells RWKV about the reviews it brought in,
  then runs the Sync button's sync, as the add-on did.
- `getProfiles`, `loadProfile` and the `gui*` actions touch windows and run
  on the main thread; every other action runs off it, and the socket I/O
  never runs on it.
- The Edit dialog of `guiEditNote` is the add-on's (Preview, Previous,
  Next, Browse), with the same saved window size.

**Why:** plan item 4 and Andrew, 2026-09-15 ("integrate AnkiConnect
natively"); tonight's other goal, a responsive UI, is why collection work
leaves the main thread and the screens no longer reset.

**Pinned by:** `qt/tests/test_ankiconnect.py` (one or more tests per
action, over HTTP on an ephemeral port against a test collection;
`test_the_action_list_is_the_addons_and_the_forks`;
`test_every_action_is_covered` fails when an action has no test;
`test_changes_refresh_the_screens_once_per_burst`,
`test_a_change_rwkv_cannot_keep_is_refreshed_on_its_own`,
`test_the_burst_refresh_waits_for_a_running_request`,
`test_note_changes_keep_the_rwkv_state`,
`test_sync_then_the_sync_button`,
`test_sync_that_brought_reviews_refreshes_rwkv_first`,
`test_sync_fails_when_a_full_sync_is_needed`).

## ankiconnect.one-algorithm

Given a request for scheduling values, AnkiConnect gives the values of the
algorithm that schedules the card (`sched.one-global-algorithm`) and never
another algorithm's:

| Value                                  | FSRS-7              | RWKV-Curve                            | RWKV-Instant               |
| -------------------------------------- | ------------------- | ------------------------------------- | -------------------------- |
| `prop:r` (`cardsInfo`, `cardsDetails`) | FSRS-7's R          | the curve's recall now                | RWKV's R, null until known |
| `prop:s`                               | FSRS-7's S90        | the curve's S90 (null without one)    | null                       |
| `prop:d` of `cardsInfo` (difficulty)   | FSRS-7's            | null                                  | null                       |
| `cardsInfo` `nextReviews`              | the 4 labels        | 4 empty strings                       | 4 empty strings            |
| `guiCurrentCard` `nextReviews`         | the buttons' labels | once RWKV-Curve gave them, else empty | empty strings              |
| `areDue`                               | as the add-on       | as the add-on                         | null for a review card     |
| `answerCards` / `gradeNow`             | answered            | answered with RWKV-Curve's intervals  | answered                   |

These are the values card info shows (`ui.card-info-one-algorithm`); the
curve is taken at the time since the card's last answered review.
`prop:d` of `cardsDetails` is the card's desired retention, as in the fork.
`findCards`, `findNotes`, `notesInfo`'s `query` and `guiBrowse` search as
the Browser does, RWKV scores prepared first for `prop:rwkv:r` and
`prop:rwkv-curve:r` searches. `cardsInfo`'s `interval` and `due`,
`getIntervals`, `getEaseFactors` and `getDeckStats` give the stored values,
as the Browser, card info and the deck list show them (the ease factor is
stored but used by none of Clanki's algorithms; under RWKV-Instant the
deck list's review counts are RWKV-Instant's scored counts).

`answerCards`, `gradeNow` and `guiAnswerCard` answer a card the way the
reviewer answers it, whatever schedules it. `answerCards` and `gradeNow`
answer through Grade Now (`sched.grade-now-rwkv-curve`), so an RWKV-Curve
card stores RWKV-Curve's interval and S90 and never FSRS-7's;
`guiAnswerCard` answers through the reviewer, as before. `answerCards`
answers the cards of the other algorithms one at a time, as the add-on
does, and its RWKV-Curve cards after them, one Grade Now per `ease`.
Given a card RWKV-Curve has no intervals for
(`sched.rwkv-curve-buttons-wait`), that card is not answered:

- `answerCards` gives `false` for it, as it did for every RWKV-Curve card
  before, and answers the other cards of the request;
- `gradeNow` grades the other cards and then fails with "gradeNow:
  RWKV-Curve has no intervals yet for card(s) `<ids>`, so they were not
  graded; the other cards were graded";
- `guiAnswerCard` gives `false`, because the reviewer ignores the answer.

**Why:** Andrew, 2026-09-15: never mix two scheduling algorithms in one
display, stat or computation; hide rather than fall back. Andrew,
2026-09-16: "Grade Now should work with all algorithms, yes" — before
that, `answerCards` gave `false` and `gradeNow` an error for every
RWKV-Curve card, because the plain answer path would have stored FSRS-7's
interval.

**Pinned by:** `qt/tests/test_ankiconnect.py`
(`test_card_algorithm_follows_the_card_preset`,
`test_prop_values_follow_the_algorithm`,
`test_next_reviews_are_hidden_for_rwkv`,
`test_are_due_is_null_for_rwkv_instant_review_cards`,
`test_answer_cards_answers_each_algorithm_as_the_reviewer`,
`test_answer_cards_gives_false_while_rwkv_curve_has_no_intervals`,
`test_grade_now_answers_each_algorithm_as_the_reviewer`,
`test_grade_now_fails_naming_the_cards_without_rwkv_curve_intervals`,
`test_gui_answer_card_is_false_while_rwkv_curve_has_no_intervals`,
`test_rwkv_retrievability_searches_are_prepared_as_in_the_browser`).

## ankiconnect.settings

Given Preferences, its AnkiConnect tab (after Review Heatmap) holds: the
on/off switch, the address (127.0.0.1 by default), the port (8765), the
allowed web origins, one per line (`http://localhost`), and the API key
(none). They are stored in the profile manager's global meta under
`ankiConnect`, with the add-on's key names, apply to every profile (like
the add-on's, whose folder all profiles share) and do not sync. A change
of on/off, address or port restarts the server at once; new origins or a
new key apply to the next request. The tab shows the server's state:
off, listening on address:port, the port in use, or another error.

AnkiConnect is off by default. The first time Clanki starts with it, the
settings come from an installed AnkiConnect add-on (its config.json with
the user's changes from its meta.json, read only) and it is on if the
add-on was enabled; without the add-on, the defaults. The add-on's
`apiLogPath` and `webTimeout` carry over without a control.

Given the port in use (another program, perhaps another Anki with
AnkiConnect) or the address missing, Clanki does not stop starting: a
tooltip says so once, the tab shows it, and Clanki tries again every 5 s
until it can listen. On Windows the port is taken exclusively, so no
other program can listen on it at the same time.

**Why:** Andrew, 2026-09-15: a Preferences tab of its own, like Review
Heatmap. Off by default because an open port that can change the
collection is a risk nobody should carry without using it; users of the
add-on keep it working with their settings.

**Pinned by:** `qt/tests/test_ankiconnect.py`
(`test_settings_defaults_and_storage`,
`test_settings_apply_restarts_only_for_a_new_address`,
`test_settings_from_the_preferences_form`,
`test_migration_takes_the_addons_settings`,
`test_migration_without_the_addon_or_with_it_disabled`,
`test_port_in_use_is_retried`).

## ankiconnect.addon-blocked

Given the AnkiConnect add-on (AnkiWeb 2055492159, the fork 1635024181, a
source install, or any add-on named AnkiConnect or AnkiConnect Extended)
installed and enabled at start-up, Clanki disables it before add-ons load,
turns the built-in AnkiConnect on and, the first time only, tells the user
so once the profile is open (a flag in the profile manager's global meta).
Given the user enables that add-on in Tools > Add-ons, or installs it
anew, a message says that Clanki already has AnkiConnect built in and has
turned it on (settings in Preferences > AnkiConnect), and the add-on stays
disabled; an update of a copy already installed stays disabled without a
message. Other add-ons enable and install as before.

**Why:** Andrew, 2026-09-15: "do the same thing as with Review Heatmap,
where it blocks the user from installing the add-on and tells him that
this functionality already exists". Both would want the same port.

**Pinned by:** `qt/tests/test_ankiconnect.py`
(`test_an_enabled_ankiconnect_addon_is_disabled`,
`test_start_up_turns_the_builtin_on_in_place_of_the_addon`,
`test_the_ankiconnect_notice_is_shown_only_once`,
`test_enabling_the_ankiconnect_addon_is_refused_with_a_message`,
`test_installing_the_ankiconnect_addon_leaves_it_disabled`,
`test_ankiconnect_addon_is_recognised_by_id_or_name`).
