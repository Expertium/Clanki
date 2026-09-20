// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! How the interface names the chance of recalling a card now (spec
//! `ui.simple-recall-wording`): the technical word "retrievability", or the
//! plain words "probability of recall". One setting, three choices, stored
//! in the collection config under [`StringKey::RecallWording`].

pub use anki_proto::config::RecallWording;

use crate::config::BoolKey;
use crate::config::StringKey;
use crate::prelude::*;

/// The setting's stored names. They are the same three words in Rust,
/// Python (`qt/aqt/ui_split.py`) and TypeScript (`ts/lib/tslib/
/// recall-wording.ts`).
const BY_MODE: &str = "by_mode";
const TECHNICAL: &str = "technical";
const PLAIN: &str = "plain";

/// The stored name of a choice.
pub fn recall_wording_name(wording: RecallWording) -> &'static str {
    match wording {
        RecallWording::ByMode => BY_MODE,
        RecallWording::Technical => TECHNICAL,
        RecallWording::Plain => PLAIN,
    }
}

/// The choice a stored name means; anything else means the default.
pub fn recall_wording_from_name(name: &str) -> RecallWording {
    match name {
        TECHNICAL => RecallWording::Technical,
        PLAIN => RecallWording::Plain,
        _ => RecallWording::ByMode,
    }
}

/// Whether the interface uses the plain words. This is the one helper the
/// Rust side resolves the setting with: plain words when the setting is
/// `plain`, or when it is `by_mode` and the UI is in Simple mode.
pub fn plain_recall_wording(wording: RecallWording, advanced_ui: bool) -> bool {
    match wording {
        RecallWording::Plain => true,
        RecallWording::Technical => false,
        RecallWording::ByMode => !advanced_ui,
    }
}

impl Collection {
    pub fn recall_wording(&self) -> RecallWording {
        recall_wording_from_name(&self.get_config_string(StringKey::RecallWording))
    }

    /// Whether the collection's screens name the chance of recall in plain
    /// words, in the current mode.
    pub fn plain_recall_wording(&self) -> bool {
        plain_recall_wording(
            self.recall_wording(),
            self.get_config_bool(BoolKey::AdvancedUi),
        )
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::collection::Collection;

    // Pins spec/ui.md#ui.simple-recall-wording
    #[test]
    fn the_setting_and_the_mode_together_choose_the_wording() {
        assert!(plain_recall_wording(RecallWording::ByMode, false));
        assert!(!plain_recall_wording(RecallWording::ByMode, true));
        assert!(!plain_recall_wording(RecallWording::Technical, false));
        assert!(!plain_recall_wording(RecallWording::Technical, true));
        assert!(plain_recall_wording(RecallWording::Plain, false));
        assert!(plain_recall_wording(RecallWording::Plain, true));
    }

    // Pins spec/ui.md#ui.simple-recall-wording
    #[test]
    fn an_unset_or_unknown_choice_reads_as_by_mode() -> Result<()> {
        let mut col = Collection::new();
        assert_eq!(col.recall_wording(), RecallWording::ByMode);
        col.set_config_string(StringKey::RecallWording, "nonsense", false)?;
        assert_eq!(col.recall_wording(), RecallWording::ByMode);
        col.set_config_string(StringKey::RecallWording, "plain", false)?;
        assert_eq!(col.recall_wording(), RecallWording::Plain);
        assert!(col.plain_recall_wording());
        col.set_config_string(StringKey::RecallWording, "technical", false)?;
        assert!(!col.plain_recall_wording());
        Ok(())
    }
}
