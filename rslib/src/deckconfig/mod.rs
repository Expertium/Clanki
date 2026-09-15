// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod fork_fields;
mod schema11;
mod service;
pub(crate) mod undo;
mod update;

pub use anki_proto::deck_config::deck_config::config::AnswerAction;
pub use anki_proto::deck_config::deck_config::config::FsrsVersion;
pub use anki_proto::deck_config::deck_config::config::LeechAction;
pub use anki_proto::deck_config::deck_config::config::NewCardGatherPriority;
pub use anki_proto::deck_config::deck_config::config::NewCardInsertOrder;
pub use anki_proto::deck_config::deck_config::config::NewCardSortOrder;
pub use anki_proto::deck_config::deck_config::config::QuestionAction;
pub use anki_proto::deck_config::deck_config::config::ReviewCardOrder;
pub use anki_proto::deck_config::deck_config::config::ReviewMix;
pub use anki_proto::deck_config::deck_config::Config as DeckConfigInner;
pub(crate) use fork_fields::deck_config_inner_for_storage;
pub(crate) use fork_fields::restore_fork_fields_from_other;
pub use schema11::DeckConfSchema11;
pub use schema11::NewCardOrderSchema11;
pub use update::UpdateDeckConfigsRequest;

/// Old deck config and cards table store 250% as 2500.
pub(crate) const INITIAL_EASE_FACTOR_THOUSANDS: u16 = (INITIAL_EASE_FACTOR * 1000.0) as u16;

use crate::config::BoolKey;
use crate::define_newtype;
use crate::prelude::*;
use crate::scheduler::states::review::INITIAL_EASE_FACTOR;

define_newtype!(DeckConfigId, i64);

pub const DEFAULT_REVIEW_FUZZ_BASE: f32 = 1.0;
pub const DEFAULT_REVIEW_FUZZ_FACTOR_SHORT: f32 = 0.15;
pub const DEFAULT_REVIEW_FUZZ_FACTOR_MID: f32 = 0.10;
pub const DEFAULT_REVIEW_FUZZ_FACTOR_LONG: f32 = 0.05;
pub const DEFAULT_REVIEW_FUZZ_ENABLED: bool = true;
pub(crate) const DEFAULT_RWKV_REVIEW_BATCH_SIZE: u32 = 512;
pub(crate) const DEFAULT_RWKV_REVIEW_REFRESH_INTERVAL: u32 = 1;
pub(crate) const DEFAULT_RWKV_REVIEW_ALLOW_SAME_DAY_REVIEW: bool = true;
pub(crate) const DEFAULT_RWKV_REVIEW_MIN_INTERVENING_REVIEWS: u32 = 5;
pub(crate) const DEFAULT_RWKV_REVIEW_MIN_ELAPSED_SECS: u32 = 30;
pub(crate) const DEFAULT_RWKV_REVIEW_FIRST_REVIEW_ELAPSED_FROM_CARD_CREATION: bool = true;
pub(crate) const DEFAULT_RWKV_REVIEW_ENFORCE_GRADE_ORDER: bool = true;

#[derive(Debug, PartialEq, Clone)]
pub struct DeckConfig {
    pub id: DeckConfigId,
    pub name: String,
    pub mtime_secs: TimestampSecs,
    pub usn: Usn,
    pub inner: DeckConfigInner,
}

/// NOTE: this does not set the default steps
// A new preset has no learning or relearning steps and runs RWKV-Curve
// (spec deck-options.new-preset-defaults). Stored presets keep their own
// values: the RWKV flag is only ever read from the stored `jschoreels.rwkv`
// bag, never from this default (`fork_fields.rs`).
const DEFAULT_DECK_CONFIG_INNER: DeckConfigInner = DeckConfigInner {
    learn_steps: Vec::new(),
    relearn_steps: Vec::new(),
    new_per_day: 20,
    // no practical review cap for new presets; the control is Advanced-only
    // (spec deck-options.new-preset-defaults)
    reviews_per_day: 9999,
    new_per_day_minimum: 0,
    initial_ease: 2.5,
    easy_multiplier: 1.3,
    hard_multiplier: 1.2,
    lapse_multiplier: 0.0,
    interval_multiplier: 1.0,
    maximum_review_interval: 36_500,
    minimum_lapse_interval: 1,
    graduating_interval_good: 1,
    graduating_interval_easy: 4,
    new_card_insert_order: NewCardInsertOrder::Due as i32,
    new_card_gather_priority: NewCardGatherPriority::Deck as i32,
    new_card_sort_order: NewCardSortOrder::Template as i32,
    // most likely to be recalled first (spec deck-options.new-preset-defaults)
    review_order: ReviewCardOrder::RetrievabilityDescending as i32,
    new_mix: ReviewMix::MixWithReviews as i32,
    interday_learning_mix: ReviewMix::MixWithReviews as i32,
    leech_action: LeechAction::TagOnly as i32,
    leech_threshold: 8,
    leech_only_if_young: false,
    rwkv_review_enabled: true,
    rwkv_review_batch_size: DEFAULT_RWKV_REVIEW_BATCH_SIZE,
    rwkv_review_refresh_interval: DEFAULT_RWKV_REVIEW_REFRESH_INTERVAL,
    rwkv_review_refresh_on_exit: false,
    rwkv_review_allow_same_day_review: DEFAULT_RWKV_REVIEW_ALLOW_SAME_DAY_REVIEW,
    rwkv_review_instant_order_enabled: false,
    rwkv_review_dynamic_preset_replay: false,
    rwkv_review_candidate_refresh_enabled: false,
    rwkv_review_min_intervening_reviews: DEFAULT_RWKV_REVIEW_MIN_INTERVENING_REVIEWS,
    rwkv_review_min_elapsed_secs: DEFAULT_RWKV_REVIEW_MIN_ELAPSED_SECS,
    rwkv_review_first_review_elapsed_from_card_creation:
        DEFAULT_RWKV_REVIEW_FIRST_REVIEW_ELAPSED_FROM_CARD_CREATION,
    rwkv_review_enforce_grade_order: DEFAULT_RWKV_REVIEW_ENFORCE_GRADE_ORDER,
    rwkv_review_minimum_reviews_per_day: 0,
    disable_autoplay: false,
    cap_answer_time_to_secs: 60,
    show_timer: false,
    stop_timer_on_answer: false,
    seconds_to_show_question: 0.0,
    seconds_to_show_answer: 0.0,
    question_action: QuestionAction::ShowAnswer as i32,
    answer_action: AnswerAction::BuryCard as i32,
    wait_for_audio: true,
    skip_question_when_replaying_answer: false,
    bury_new: false,
    bury_reviews: false,
    bury_interday_learning: false,
    fsrs_params_4: vec![],
    fsrs_params_5: vec![],
    fsrs_params_6: vec![],
    fsrs_params_7: vec![],
    fsrs_minimum_interval_secs: 1,
    fsrs_version: FsrsVersion::Seven as i32,
    desired_retention: 0.9,
    other: Vec::new(),
    historical_retention: 0.9,
    param_search: String::new(),
    ignore_revlogs_before_date: String::new(),
    easy_days_percentages: Vec::new(),
    review_fuzz_base: None,
    review_fuzz_factor_short: None,
    review_fuzz_factor_mid: None,
    review_fuzz_factor_long: None,
    review_fuzz_enabled: None,
    max_same_day_reviews: None,
};

impl Default for DeckConfig {
    fn default() -> Self {
        DeckConfig {
            id: DeckConfigId(0),
            name: "".to_string(),
            mtime_secs: Default::default(),
            usn: Default::default(),
            inner: DeckConfigInner {
                easy_days_percentages: vec![1.0; 7],
                ..DEFAULT_DECK_CONFIG_INNER
            },
        }
    }
}

/// The parameters Clanki runs FSRS-7 with (spec sched.fsrs7-only): the given
/// FSRS-7 parameters when they are 34 finite values, else the FSRS-7 defaults.
/// Clanki has no other FSRS model; stored FSRS-6/5/4 parameters are ignored.
pub(crate) fn effective_fsrs7_params(params: &[f32]) -> &[f32] {
    if params.len() == fsrs::DEFAULT_PARAMETERS.len() && params.iter().all(|w| w.is_finite()) {
        params
    } else {
        &fsrs::DEFAULT_PARAMETERS
    }
}

impl DeckConfig {
    pub(crate) fn set_modified(&mut self, usn: Usn) {
        self.mtime_secs = TimestampSecs::now();
        self.usn = usn;
    }

    /// The FSRS-7 parameters this preset runs with: its stored FSRS-7
    /// parameters, or the FSRS-7 defaults when it has none (never optimized)
    /// or they are unusable. The stored version and the FSRS-6/5/4 slots are
    /// kept for other clients but not read (spec sched.fsrs7-only).
    pub fn fsrs_params(&self) -> &[f32] {
        effective_fsrs7_params(&self.inner.fsrs_params_7)
    }

    /// The preset's "Max number of same-day reviews". It applies only while
    /// the preset has no learning steps; with steps, the steps decide the
    /// same-day reviews (spec sched.max-same-day-reviews).
    pub(crate) fn effective_max_same_day_reviews(&self) -> Option<u32> {
        if self.inner.learn_steps.is_empty() {
            self.inner.max_same_day_reviews
        } else {
            None
        }
    }

    /// Clear the FSRS 6.0 params, along with the 5.0 and 4.x fallbacks.
    pub(crate) fn clear_fsrs_params(&mut self) {
        self.inner.fsrs_params_4.clear();
        self.inner.fsrs_params_5.clear();
        self.inner.fsrs_params_6.clear();
    }
}

impl Collection {
    /// If fallback is true, guaranteed to return a deck config.
    pub fn get_deck_config(
        &self,
        dcid: DeckConfigId,
        fallback: bool,
    ) -> Result<Option<DeckConfig>> {
        if let Some(conf) = self.storage.get_deck_config(dcid)? {
            return Ok(Some(conf));
        }
        if fallback {
            if let Some(conf) = self.storage.get_deck_config(DeckConfigId(1))? {
                return Ok(Some(conf));
            }
            // if even the default deck config is missing, just return the defaults
            Ok(Some(DeckConfig::default()))
        } else {
            Ok(None)
        }
    }
}

impl Collection {
    pub(crate) fn add_or_update_deck_config(&mut self, config: &mut DeckConfig) -> Result<()> {
        let usn = Some(self.usn()?);

        if config.id.0 == 0 {
            self.add_deck_config_inner(config, usn)
        } else {
            let original = self
                .storage
                .get_deck_config(config.id)?
                .or_not_found(config.id)?;
            self.update_deck_config_inner(config, original, usn)
        }
    }

    /// Used by the old import code; if provided id is non-zero, will add
    /// instead of ignoring. Does not support undo.
    pub(crate) fn add_or_update_deck_config_legacy(
        &mut self,
        config: &mut DeckConfig,
    ) -> Result<()> {
        let usn = self.usn()?;

        if config.id.0 == 0 {
            self.add_deck_config_inner(config, Some(usn))
        } else {
            config.set_modified(usn);
            self.storage
                .add_or_update_deck_config_with_existing_id(config)
        }
    }

    /// Assigns an id and adds to DB. If usn is provided, modification time and
    /// usn will be updated.
    pub(crate) fn add_deck_config_inner(
        &mut self,
        config: &mut DeckConfig,
        usn: Option<Usn>,
    ) -> Result<()> {
        if let Some(usn) = usn {
            config.set_modified(usn);
        }
        config.id.0 = TimestampMillis::now().0;
        self.add_deck_config_undoable(config)
    }

    /// Update an existing deck config. If usn is provided, modification time
    /// and usn will be updated.
    pub(crate) fn update_deck_config_inner(
        &mut self,
        config: &mut DeckConfig,
        original: DeckConfig,
        usn: Option<Usn>,
    ) -> Result<()> {
        if config == &original {
            return Ok(());
        }
        if let Some(usn) = usn {
            config.set_modified(usn);
        }
        self.update_deck_config_undoable(config, original)
    }

    /// The removed collection-wide "Skip learning/relearning queues with
    /// FSRS/RWKV" switch becomes a limit of 0 same-day reviews on every
    /// preset, and the switch is cleared, so this runs once (spec
    /// sched.max-same-day-reviews).
    pub(crate) fn migrate_learning_queues_switch(&mut self) -> Result<()> {
        if !self.get_config_bool(BoolKey::FsrsLearningQueuesDisabled) {
            return Ok(());
        }
        self.transact_no_undo(|col| {
            let usn = col.usn()?;
            for original in col.storage.all_deck_config()? {
                if original.inner.max_same_day_reviews == Some(0) {
                    continue;
                }
                let mut config = original.clone();
                config.inner.max_same_day_reviews = Some(0);
                col.update_deck_config_inner(&mut config, original, Some(usn))?;
            }
            col.set_config_bool_inner(BoolKey::FsrsLearningQueuesDisabled, false)?;
            Ok(())
        })
    }

    /// Remove a deck configuration. This will force a full sync.
    pub(crate) fn remove_deck_config_inner(&mut self, dcid: DeckConfigId) -> Result<()> {
        require!(dcid.0 != 1, "can't delete default conf");
        let conf = self.storage.get_deck_config(dcid)?.or_not_found(dcid)?;
        self.set_schema_modified()?;
        self.remove_deck_config_undoable(conf)
    }
}

/// There was a period of time when the deck options screen was allowing
/// 0/NaN to be persisted, so we need to check the values are within
/// valid bounds when reading from the DB.
pub(crate) fn ensure_deck_config_values_valid(config: &mut DeckConfigInner) {
    let default = DEFAULT_DECK_CONFIG_INNER;
    ensure_u32_valid(&mut config.new_per_day, default.new_per_day, 0, 9999);
    ensure_u32_valid(
        &mut config.reviews_per_day,
        default.reviews_per_day,
        0,
        9999,
    );
    ensure_u32_valid(
        &mut config.new_per_day_minimum,
        default.new_per_day_minimum,
        0,
        9999,
    );
    ensure_f32_valid(&mut config.initial_ease, default.initial_ease, 1.31, 5.0);
    ensure_f32_valid(
        &mut config.easy_multiplier,
        default.easy_multiplier,
        1.0,
        5.0,
    );
    ensure_f32_valid(
        &mut config.hard_multiplier,
        default.hard_multiplier,
        0.5,
        1.3,
    );
    ensure_f32_valid(
        &mut config.lapse_multiplier,
        default.lapse_multiplier,
        0.0,
        1.0,
    );
    ensure_f32_valid(
        &mut config.interval_multiplier,
        default.interval_multiplier,
        0.5,
        2.0,
    );
    ensure_u32_valid(
        &mut config.maximum_review_interval,
        default.maximum_review_interval,
        1,
        36_500,
    );
    ensure_u32_valid(
        &mut config.fsrs_minimum_interval_secs,
        default.fsrs_minimum_interval_secs,
        1,
        36_500 * 86_400,
    );
    ensure_u32_valid(
        &mut config.rwkv_review_batch_size,
        default.rwkv_review_batch_size,
        64,
        8192,
    );
    ensure_u32_valid(
        &mut config.rwkv_review_refresh_interval,
        default.rwkv_review_refresh_interval,
        1,
        10_000,
    );
    ensure_u32_valid(
        &mut config.rwkv_review_min_intervening_reviews,
        default.rwkv_review_min_intervening_reviews,
        0,
        10_000,
    );
    ensure_u32_valid(
        &mut config.rwkv_review_min_elapsed_secs,
        default.rwkv_review_min_elapsed_secs,
        0,
        86_400,
    );
    ensure_u32_valid(
        &mut config.rwkv_review_minimum_reviews_per_day,
        default.rwkv_review_minimum_reviews_per_day,
        0,
        9999,
    );
    ensure_u32_valid(
        &mut config.minimum_lapse_interval,
        default.minimum_lapse_interval,
        1,
        36_500,
    );
    ensure_u32_valid(
        &mut config.graduating_interval_good,
        default.graduating_interval_good,
        1,
        36_500,
    );
    ensure_u32_valid(
        &mut config.graduating_interval_easy,
        default.graduating_interval_easy,
        1,
        36_500,
    );
    ensure_u32_valid(
        &mut config.leech_threshold,
        default.leech_threshold,
        1,
        9999,
    );
    ensure_u32_valid(
        &mut config.cap_answer_time_to_secs,
        default.cap_answer_time_to_secs,
        1,
        9999,
    );
    ensure_f32_valid(
        &mut config.desired_retention,
        default.desired_retention,
        0.1,
        0.99,
    );
    ensure_f32_valid(
        &mut config.historical_retention,
        default.historical_retention,
        0.7,
        0.97,
    )
}

fn ensure_f32_valid(val: &mut f32, default: f32, min: f32, max: f32) {
    if val.is_nan() || *val < min || *val > max {
        *val = default;
    }
}

fn ensure_u32_valid(val: &mut u32, default: u32, min: u32, max: u32) {
    if *val < min || *val > max {
        *val = default;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collection::CollectionBuilder;

    // Pins spec/scheduling.md#sched.max-same-day-reviews: the removed switch
    // becomes a limit of 0 on every preset, once, when the collection opens.
    #[test]
    fn learning_queues_switch_becomes_a_zero_limit_on_open() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("col.anki2");
        {
            let mut col = CollectionBuilder::new(&path).build()?;
            let mut second = DeckConfig::default();
            col.add_or_update_deck_config(&mut second)?;
            col.set_config_bool_inner(BoolKey::FsrsLearningQueuesDisabled, true)?;
            col.close(None)?;
        }
        let mut col = CollectionBuilder::new(&path).build()?;
        assert!(!col.get_config_bool(BoolKey::FsrsLearningQueuesDisabled));
        let configs = col.storage.all_deck_config()?;
        assert_eq!(configs.len(), 2);
        for config in &configs {
            assert_eq!(config.inner.max_same_day_reviews, Some(0));
        }

        // a later change is not undone by the next open
        let mut config = configs[0].clone();
        config.inner.max_same_day_reviews = Some(3);
        col.add_or_update_deck_config(&mut config)?;
        col.close(None)?;
        let col = CollectionBuilder::new(&path).build()?;
        let config = col.get_deck_config(config.id, false)?.unwrap();
        assert_eq!(config.inner.max_same_day_reviews, Some(3));
        Ok(())
    }

    // Pins spec/scheduling.md#sched.max-same-day-reviews: the limit is stored
    // and synced with the preset; unset means no limit.
    #[test]
    fn max_same_day_reviews_survives_storage_and_schema11() -> Result<()> {
        let mut col = Collection::new();
        let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(config.inner.max_same_day_reviews, None);
        config.inner.max_same_day_reviews = Some(2);
        col.add_or_update_deck_config(&mut config)?;
        let stored = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(stored.inner.max_same_day_reviews, Some(2));
        let legacy = DeckConfSchema11::from(stored);
        let json = serde_json::to_string(&legacy)?;
        let back = DeckConfig::from(serde_json::from_str::<DeckConfSchema11>(&json)?);
        assert_eq!(back.inner.max_same_day_reviews, Some(2));
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.new-preset-defaults
    #[test]
    fn new_preset_has_no_steps_and_runs_rwkv_curve() {
        let config = DeckConfig::default();
        assert!(config.inner.learn_steps.is_empty());
        assert!(config.inner.relearn_steps.is_empty());
        assert!(config.inner.rwkv_review_enabled);
        assert!(!config.inner.rwkv_review_instant_order_enabled);
        assert_eq!(config.inner.leech_action, LeechAction::TagOnly as i32);
        assert_eq!(config.inner.reviews_per_day, 9999);
        assert_eq!(
            config.inner.review_order,
            ReviewCardOrder::RetrievabilityDescending as i32
        );
        // the legacy JSON default (Python add_config / restore_to_default)
        // agrees
        let legacy = DeckConfig::from(DeckConfSchema11::default());
        assert_eq!(legacy.inner.leech_action, LeechAction::TagOnly as i32);
        assert_eq!(legacy.inner.reviews_per_day, 9999);
        assert_eq!(
            legacy.inner.review_order,
            ReviewCardOrder::RetrievabilityDescending as i32
        );
    }

    // Pins spec/deck-options.md#deck-options.new-preset-defaults: a fresh
    // collection's default preset gets the new-preset defaults.
    #[test]
    fn fresh_collection_starts_with_new_preset_defaults() -> Result<()> {
        let col = CollectionBuilder::default().build()?;
        let config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert!(config.inner.learn_steps.is_empty());
        assert!(config.inner.relearn_steps.is_empty());
        assert!(config.inner.rwkv_review_enabled);
        assert_eq!(config.inner.leech_action, LeechAction::TagOnly as i32);
        assert_eq!(config.inner.reviews_per_day, 9999);
        assert_eq!(
            config.inner.review_order,
            ReviewCardOrder::RetrievabilityDescending as i32
        );
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.new-preset-defaults: a stored
    // preset without the RWKV flag keeps reading as FSRS, whatever the
    // default for new presets is.
    #[test]
    fn stored_preset_without_rwkv_flag_stays_off() -> Result<()> {
        let mut legacy = serde_json::to_value(DeckConfSchema11::default())?;
        let object = legacy.as_object_mut().unwrap();
        object.remove("rwkvReviewEnabled");
        object.remove("jschoreels.rwkv");
        object.insert(
            "new".into(),
            serde_json::json!({
                "bury": false, "delays": [1.0, 10.0], "initialFactor": 2500,
                "ints": [1, 4, 0], "order": 1, "perDay": 20,
            }),
        );
        let legacy: DeckConfSchema11 = serde_json::from_value(legacy)?;
        let config = DeckConfig::from(legacy);
        assert!(!config.inner.rwkv_review_enabled);
        assert_eq!(config.inner.learn_steps, vec![1.0, 10.0]);
        Ok(())
    }

    /// Trained FSRS-6 parameters, as an FSRS-6 optimizer would store them.
    fn trained_fsrs6_params() -> Vec<f32> {
        fsrs::FSRS6_DEFAULT_PARAMETERS
            .iter()
            .map(|w| w * 1.1)
            .collect()
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: a preset that was never
    // optimized for FSRS-7 runs the FSRS-7 defaults, whatever its stored
    // version and FSRS-6 parameters. The FSRS-6 slot stays as stored.
    #[test]
    fn fsrs_params_without_fsrs7_params_are_the_fsrs7_defaults() {
        for version in [
            FsrsVersion::Six,
            FsrsVersion::Seven,
            FsrsVersion::Five,
            FsrsVersion::Four,
        ] {
            let mut config = DeckConfig::default();
            config.inner.fsrs_version = version as i32;
            config.inner.fsrs_params_6 = trained_fsrs6_params();
            config.inner.fsrs_params_5 = vec![1.5_f32; 19];
            config.inner.fsrs_params_4 = vec![1.5_f32; 17];
            assert!(config.inner.fsrs_params_7.is_empty());

            assert_eq!(config.fsrs_params(), &fsrs::DEFAULT_PARAMETERS[..]);
            assert_eq!(config.inner.fsrs_params_6, trained_fsrs6_params());
            assert_eq!(config.inner.fsrs_version, version as i32);
        }

        // a preset without any parameters too
        assert_eq!(
            DeckConfig::default().fsrs_params(),
            &fsrs::DEFAULT_PARAMETERS[..]
        );
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: 34 valid FSRS-7 parameters run
    // as stored, even when the stored version is FSRS-6.
    #[test]
    fn fsrs_params_are_the_fsrs7_params_whatever_the_stored_version() {
        let mut config = DeckConfig::default();
        config.inner.fsrs_version = FsrsVersion::Six as i32;
        config.inner.fsrs_params_6 = trained_fsrs6_params();
        config.inner.fsrs_params_7 = vec![2.0_f32; 34];

        assert_eq!(config.fsrs_params(), &[2.0_f32; 34]);
        assert_eq!(config.inner.fsrs_params_6, trained_fsrs6_params());
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: FSRS-7 parameters that are not
    // 34 finite values are unusable, so the preset runs the FSRS-7 defaults.
    #[test]
    fn unusable_fsrs7_params_give_the_fsrs7_defaults() {
        let mut with_nan = vec![2.0_f32; 34];
        with_nan[5] = f32::NAN;
        let mut with_infinity = vec![2.0_f32; 34];
        with_infinity[33] = f32::INFINITY;
        for params in [
            with_nan,
            with_infinity,
            vec![1.0_f32, 2.0, 3.0],
            vec![2.0_f32; 35],
            trained_fsrs6_params(),
        ] {
            let mut config = DeckConfig::default();
            config.inner.fsrs_version = FsrsVersion::Seven as i32;
            config.inner.fsrs_params_6 = trained_fsrs6_params();
            config.inner.fsrs_params_7 = params.clone();

            assert_eq!(config.fsrs_params(), &fsrs::DEFAULT_PARAMETERS[..]);
            assert_eq!(config.inner.fsrs_params_6, trained_fsrs6_params());
            assert_eq!(config.inner.fsrs_params_7.len(), params.len());
        }
    }

    #[test]
    fn maximum_rwkv_batch_size_is_preserved() {
        let mut config = DeckConfig::default().inner;
        config.rwkv_review_batch_size = 8192;

        ensure_deck_config_values_valid(&mut config);

        assert_eq!(config.rwkv_review_batch_size, 8192);
    }

    #[test]
    fn invalid_rwkv_batch_size_uses_default() {
        let mut config = DeckConfig::default().inner;
        config.rwkv_review_batch_size = 8193;

        ensure_deck_config_values_valid(&mut config);

        assert_eq!(
            config.rwkv_review_batch_size,
            DEFAULT_RWKV_REVIEW_BATCH_SIZE
        );
    }
}
