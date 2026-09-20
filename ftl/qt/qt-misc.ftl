qt-misc-addon-will-be-installed-when-a = Add-on will be installed when a profile is opened.
qt-misc-addons = Add-ons
qt-misc-all-cards-notes-and-media-for = All cards, notes, and media for this profile will be deleted. Are you sure?
qt-misc-all-cards-notes-and-media-for2 = All cards, notes, and media for the profile "{ $name }" will be deleted. Are you sure?
qt-misc-anki-updatedanki-has-been-released = <h1>Clanki Updated</h1>Clanki { $val } has been released.<br><br>
qt-misc-automatic-syncing-and-backups-have-been = Backup successfully restored. Automatic syncing and backups have been disabled for now. To enable them again, close the profile or restart Clanki.
qt-misc-back-side-only = Back Side Only
qt-misc-backing-up = Backing Up...
qt-misc-browse = Browse
qt-misc-change-note-type-ctrlandn = Change Note Type (Ctrl+N)
qt-misc-check-the-files-in-the-media = Check the files in the media directory
qt-misc-choose-deck = Choose Deck
qt-misc-choose-note-type = Choose Note Type
qt-misc-closing = Closing...
qt-misc-configure-interface-language-and-options = Configure interface language and options
qt-misc-copy-to-clipboard = Copy to Clipboard
qt-misc-create-filtered-deck = Create Filtered Deck...
qt-misc-debug-console = Debug Console
qt-misc-deck-will-be-imported-when-a = Deck will be imported when a profile is opened.
qt-misc-empty-cards = Empty Cards...
qt-misc-error-during-startup = Error during startup: { $val }
qt-misc-ignore-this-update = Ignore this update
qt-misc-in-order-to-ensure-your-collection = In order to ensure your collection works correctly when moved between devices, Clanki requires your computer's internal clock to be set correctly. The internal clock can be wrong even if your system is showing the correct local time.<br><br>Please go to the time settings on your computer and check the following:<br><br>- AM/PM<br>- Clock drift<br>- Day, month and year<br>- Timezone<br>- Daylight savings<br><br>Difference to correct time: { $val }.
qt-misc-invalid-property-found-on-card-please = Invalid property found on card. Please use Tools>Check Database, and if the problem comes up again, please ask on the support site.
qt-misc-loading = Loading...
qt-misc-manage = Manage
qt-misc-manage-note-types = Manage Note Types
qt-misc-name-exists = Name exists.
qt-misc-non-unicode-text = <non-unicode text>
qt-misc-optimizing = Optimizing...
qt-misc-unable-to-record =
    Unable to record. Please ensure a microphone is connected, and Clanki has permission to use the microphone.
    If other programs are using your microphone, closing them may help.
    
    Original error: { $error }
qt-misc-please-ensure-a-profile-is-open = Please ensure a profile is open and Clanki is not busy, then try again.
qt-misc-please-select-1-card = (please select 1 card)
qt-misc-please-select-a-deck = Please select a deck.
qt-misc-please-use-fileimport-to-import-this = Please use File>Import to import this file.
qt-misc-processing = Processing...
qt-misc-replace-your-collection-with-an-earlier2 = Replace your collection with an earlier backup from { $val }?
qt-misc-revert-to-backup = Revert to backup
# please do not change the quote character, and please only change the font name if you have confirmed the new name is a valid Windows font
qt-misc-segoe-ui = "Segoe UI"
qt-misc-shift-key-was-held-down-skipping = Shift key was held down. Skipping automatic syncing and add-on loading.
qt-misc-shortcut-key-left-arrow = Shortcut key: Left arrow
qt-misc-shortcut-key-right-arrow-or-enter = Shortcut key: Right arrow or Enter
qt-misc-stats = Stats
qt-misc-study-deck = Study Deck...
qt-misc-sync = Sync
qt-misc-target-deck-ctrlandd = Target Deck (Ctrl+D)
qt-misc-the-following-character-can-not-be = The following character can not be used: { $val }
qt-misc-the-requested-change-will-require-a = The requested change will require a full upload of the database when you next synchronize your collection. If you have reviews or other changes waiting on another device that haven't been synchronized here yet, they will be lost. Continue?
qt-misc-there-must-be-at-least-one = There must be at least one profile.
qt-misc-this-file-exists-are-you-sure = This file exists. Are you sure you want to overwrite it?
qt-misc-unable-to-access-anki-media-folder = Unable to access Clanki media folder. The permissions on your system's temporary folder may be incorrect.
qt-misc-unexpected-response-code = Unexpected response code: { $val }
qt-misc-would-you-like-to-download-it = Would you like to download it now?
qt-misc-downloading-update = Downloading update: { $count }MB/{ $total }MB
qt-misc-your-collection-file-appears-to-be = Your collection file appears to be corrupt. This can happen when the file is copied or moved while Clanki is open, or when the collection is stored on a network or cloud drive. If problems persist after restarting your computer, please open an automatic backup from the profile screen.
qt-misc-your-computers-storage-may-be-full = Your computer's storage may be full. Please delete some unneeded files, then try again.
qt-misc-your-firewall-or-antivirus-program-is = Your firewall or antivirus program is preventing Clanki from creating a connection to itself. Please add an exception for Clanki.
qt-misc-error = Error
qt-misc-no-temp-folder = No usable temporary folder found. Make sure C:\\temp exists or TEMP in your environment points to a valid, writable folder.
qt-misc-incompatible-video-driver = Your video driver is incompatible. Please start Clanki again, and Clanki will switch to a slower, more compatible mode.
qt-misc-error-loading-graphics-driver = Error loading '{ $mode }' graphics driver. Please start Clanki again to try the next driver. { $context }
qt-misc-anki-is-running = Clanki Already Running
qt-misc-if-instance-is-not-responding = If the existing instance of Clanki is not responding, please close it using your task manager, or restart your computer.
qt-misc-second =
    { $count ->
        [one] { $count } second
       *[other] { $count } seconds
    }
qt-misc-layout-auto-enabled = Responsive layout enabled
qt-misc-layout-vertical-enabled = Vertical layout enabled
qt-misc-layout-horizontal-enabled = Horizontal layout enabled
qt-misc-rwkv-filtered-deck-preparation-failed = { $algorithm } retrievability scores could not be prepared, so the filtered deck was not rebuilt.
# The same message in Simple mode, which never says "retrievability".
qt-misc-rwkv-filtered-deck-preparation-failed-simple = { $algorithm } probability of recall scores could not be prepared, so the filtered deck was not rebuilt.
# Shown instead of the answer buttons until RWKV-Curve has calculated the
# card's intervals. Plain words, no algorithm name (spec ui.plain-progress-text).
qt-misc-rwkv-curve-intervals-pending = Getting this card ready…
# RWKV cannot run because its model file is missing or does not load.
qt-misc-rwkv-model-not-found = RWKV model not found
# Shown instead of the answer buttons when RWKV-Curve calculated the card but
# gave no interval for a button. Waiting longer cannot help.
qt-misc-rwkv-curve-no-interval = RWKV-Curve has no interval for this card. The answer buttons stay hidden, because FSRS-7 intervals must never stand in for RWKV-Curve intervals.
# Shown instead of the answer buttons when RWKV-Curve did not give the card's
# intervals in time. $seconds is how long Clanki waited.
qt-misc-rwkv-curve-intervals-timed-out = RWKV-Curve did not give the intervals of this card in { $seconds } seconds. Clanki could not load the RWKV-Curve state. Try again, or restart Clanki, or choose FSRS-7 for this deck in deck options. FSRS-7 intervals never stand in.
# Button in the message above. It waits for RWKV-Curve again, on the same card.
qt-misc-rwkv-curve-intervals-try-again = Try again
# The progress window shown ONCE, at the first start-up after the update,
# while Clanki converts a saved RWKV state cache that an older version
# wrote. Andrew chose this wording himself (2026-09-16); keep it as written.
qt-misc-rwkv-state-upgrade-title = One-time update
qt-misc-rwkv-state-upgrade-label = Clanki is reorganising its saved review data so it can start faster. This happens once and can take some time.
# RWKV-Instant has not scored the deck yet, so its reviews are not shown.
# Plain words, no algorithm name (spec ui.plain-progress-text).
qt-misc-rwkv-instant-scores-pending = Choosing your reviews…
# Grade Now left these cards unanswered: RWKV-Curve gave no intervals for them.
qt-misc-rwkv-curve-grade-now-skipped =
    { $cards ->
        [one] { $cards } card was not graded: RWKV-Curve has no intervals for it yet.
       *[other] { $cards } cards were not graded: RWKV-Curve has no intervals for them yet.
    }
# Shown when the automatic pass that stores FSRS-7's predictions of past
# reviews fails. Without it the model-quality graphs have no FSRS-7 series,
# and an empty series alone does not say that anything went wrong.
qt-misc-fsrs-predictions-pass-failed = Clanki could not store FSRS-7's predictions of your past reviews, so the model comparison graphs cannot show FSRS-7. The reason is in the log file.
# Shown when the RWKV calibration pass cannot record RWKV-Curve's value for
# a past review. The pass stops before it starts, rather than replaying the
# whole history and writing nothing.
qt-misc-rwkv-curve-not-recorded = RWKV-Curve's predictions of your past reviews were not recorded, because this version of Clanki cannot calculate them. Nothing was recomputed.
qt-misc-rwkv-model-missing = The RWKV model file is missing or does not load, so RWKV cannot schedule your cards. Reinstall Clanki, or choose FSRS-7 as the algorithm in deck options (Advanced mode).

## deprecated- these strings will be removed in the future, and do not need
## to be translated

qt-misc-replace-your-collection-with-an-earlier = Replace your collection with an earlier backup?

## UI mode (Clanki)

qt-misc-ui-mode = UI mode
qt-misc-ui-mode-simple = Simple
qt-misc-ui-mode-advanced = Advanced
qt-misc-ui-mode-tooltip = Simple shows the essential settings. Advanced shows everything.

## Progress windows of RWKV's background work, in plain words (spec
## ui.plain-progress-text): no "cache", "state", "calibration" or "delta".

# Title of the window while Clanki reads the review history into RWKV.
qt-misc-review-history-title = Getting Ready
qt-misc-review-history-reading = Reading your review history
qt-misc-review-history-updating = Updating from your latest reviews
qt-misc-review-history-loading = Loading your review history
qt-misc-review-history-repairing = Updating your review history
qt-misc-review-history-after-sync = Adding the reviews from the sync...
# A step of the work, then how far it is, e.g.
# "Reading your review history: 425,984 of 656,459 reviews, about 29s left".
qt-misc-review-history-progress = { $step }: { $done } of { $total } reviews, about { $remaining } left
# The same before the first reviews are done, when no time is known yet.
qt-misc-review-history-progress-start = { $step }: { $done } of { $total } reviews
# Title and text of the window while Clanki prepares the model-quality graphs.
qt-misc-stats-data-title = Preparing Stats
qt-misc-stats-data-preparing = Preparing the Stats graphs
# Title and text of the window while RWKV-Curve calculates new due dates.
qt-misc-rwkv-curve-reschedule-title = Reschedule
qt-misc-rwkv-curve-reschedule-preparing = Calculating new due dates...
qt-misc-rwkv-curve-reschedule-failed = Rescheduling with RWKV-Curve could not be started.
qt-misc-stats-data-ready = The Stats graphs are ready.
qt-misc-stats-data-failed = The Stats graphs could not be prepared.
qt-misc-review-history-ready = Your review history is ready.
qt-misc-review-history-failed = Your review history could not be read.
qt-misc-rwkv-curve-reschedule-error = Rescheduling with RWKV-Curve failed.
qt-misc-review-history-collecting = Collecting your reviews
qt-misc-rwkv-curve-rescheduled =
    { $count ->
        [one] RWKV-Curve rescheduled { $count } card.
       *[other] RWKV-Curve rescheduled { $count } cards.
    }
