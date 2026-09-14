// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use strum::IntoStaticStr;

use crate::prelude::*;

#[derive(Debug, Clone, Copy, IntoStaticStr)]
#[strum(serialize_all = "camelCase")]
pub enum BoolKey {
    ApplyAllParentLimits,
    BrowserTableShowNotesMode,
    CardCountsSeparateInactive,
    CollapseCardState,
    CollapseDecks,
    CollapseFlags,
    CollapseNotetypes,
    CollapseSavedSearches,
    CollapseTags,
    CollapseToday,
    FutureDueShowBacklog,
    HideAudioPlayButtons,
    IgnoreAccentsInSearch,
    InterruptAudioWhenAnswering,
    NewCardsIgnoreReviewLimit,
    PasteImagesAsPng,
    PasteStripsFormatting,
    RenderLatex,
    PreviewBothSides,
    RestorePositionBrowser,
    RestorePositionReviewer,
    ResetCountsBrowser,
    ResetCountsReviewer,
    RandomOrderReposition,
    Sched2021,
    ShiftPositionOfExistingCards,
    MergeNotetypes,
    WithScheduling,
    WithDeckConfigs,
    Fsrs,
    FsrsHealthCheck,
    FsrsLegacyEvaluate,
    LoadBalancerEnabled,
    FsrsShortTermWithStepsEnabled,
    FsrsLearningQueuesDisabled,
    ShowFuzzDeltaAboveAnswerButtons,
    DeckOptionsAdvanced,
    #[strum(to_string = "normalize_note_text")]
    NormalizeNoteText,
    #[strum(to_string = "dayLearnFirst")]
    ShowDayLearningCardsFirst,
    #[strum(to_string = "estTimes")]
    ShowIntervalsAboveAnswerButtons,
    #[strum(to_string = "dueCounts")]
    ShowRemainingDueCountsInStudy,
    #[strum(to_string = "addToCur")]
    AddingDefaultsToCurrentDeck,
}

impl Collection {
    pub fn get_config_bool(&self, key: BoolKey) -> bool {
        match key {
            BoolKey::FsrsLearningQueuesDisabled => self.get_config_optional(key).unwrap_or(false),

            // some keys default to true
            BoolKey::InterruptAudioWhenAnswering
            | BoolKey::ShowIntervalsAboveAnswerButtons
            | BoolKey::AddingDefaultsToCurrentDeck
            | BoolKey::FutureDueShowBacklog
            | BoolKey::ShowRemainingDueCountsInStudy
            | BoolKey::CardCountsSeparateInactive
            | BoolKey::RestorePositionBrowser
            | BoolKey::RestorePositionReviewer
            | BoolKey::NormalizeNoteText => self.get_config_optional(key).unwrap_or(true),

            // The load balancer is always on; a stored `false` from an earlier
            // build is ignored (spec sched.fuzz-always-on).
            BoolKey::LoadBalancerEnabled => true,

            // other options default to false
            other => self.get_config_default(other),
        }
    }

    pub fn set_config_bool(
        &mut self,
        key: BoolKey,
        value: bool,
        undoable: bool,
    ) -> Result<OpOutput<()>> {
        let op = if undoable {
            Op::UpdateConfig
        } else {
            Op::SkipUndo
        };
        self.transact(op, |col| {
            col.set_config(key, &value)?;
            Ok(())
        })
    }
}

impl Collection {
    pub(crate) fn set_config_bool_inner(&mut self, key: BoolKey, value: bool) -> Result<bool> {
        self.set_config(key, &value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pins spec/scheduling.md#sched.fuzz-always-on
    #[test]
    fn load_balancer_is_always_on() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::LoadBalancerEnabled, false, false)?;
        assert!(col.get_config_bool(BoolKey::LoadBalancerEnabled));
        Ok(())
    }

    #[test]
    fn fsrs_learning_queue_bypass_defaults_to_disabled() {
        let col = Collection::new();

        assert!(!col.get_config_bool(BoolKey::FsrsLearningQueuesDisabled));
    }
}
