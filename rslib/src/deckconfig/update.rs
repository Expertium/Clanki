// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Updating configs in bulk, from the deck options screen.

use std::collections::HashMap;
use std::collections::HashSet;
use std::iter;

use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
use anki_proto::deck_config::deck_configs_for_update::ConfigWithExtra;
use anki_proto::deck_config::deck_configs_for_update::CurrentDeck;
use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use anki_proto::deck_config::UpdateDeckConfigsMode;
use anki_proto::decks::deck::normal::DayLimit;
use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS;
use tracing::debug;
use tracing::warn;

use super::algorithm::SchedulingAlgorithm;
use super::FsrsVersion;
use crate::config::I32ConfigKey;
use crate::config::StringKey;
use crate::decks::NormalDeck;
use crate::prelude::*;
use crate::scheduler::fsrs::batch::ComputeParamsBatchInput;
use crate::scheduler::fsrs::memory_state::ComputeMemoryPresetProgress;
use crate::scheduler::fsrs::memory_state::ComputeMemoryProgress;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateEntry;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateRequest;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::HISTORICAL_RETENTION;
use crate::scheduler::states::fuzz::StoredReviewFuzzConfig;
use crate::search::JoinSearches;
use crate::search::Negated;
use crate::search::Node;
use crate::search::SearchNode;
use crate::search::StateKind;
use crate::storage::comma_separated_ids;

#[derive(Debug, Clone)]
pub struct UpdateDeckConfigsRequest {
    pub target_deck_id: DeckId,
    /// Deck will be set to last provided deck config.
    pub configs: Vec<DeckConfig>,
    pub removed_config_ids: Vec<DeckConfigId>,
    pub mode: UpdateDeckConfigsMode,
    pub limits: Limits,
    pub new_cards_ignore_review_limit: bool,
    pub fsrs: bool,
    pub load_balancer_enabled: bool,
    pub fsrs_short_term_with_steps_enabled: bool,
    pub review_fuzz_config: StoredReviewFuzzConfig,
}

impl Collection {
    /// Clanki runs FSRS-7 only (spec sched.fsrs7-only). Once, when a
    /// collection opens, the cards of every preset whose parameters changed
    /// with that rule get their memory states computed again, with the
    /// FSRS-7 parameters the preset now runs with: presets that were never
    /// optimized (they ran the FSRS-6 defaults) and presets that ran FSRS-6,
    /// FSRS-5 or FSRS-4.5 parameters. Due dates are not changed, and the
    /// stored parameters of every version stay as they are.
    ///
    /// While FSRS is off there is nothing to migrate, and nothing is written:
    /// writing the done flag would mark a new, empty collection as modified,
    /// and its first sync would then need a full sync (upstream issue #5109).
    /// Turning FSRS on computes every memory state again anyway; the next open
    /// then runs the migration and sets the flag.
    pub(crate) fn migrate_to_fsrs7_only(&mut self) -> Result<()> {
        if self.get_config_bool(BoolKey::Fsrs7OnlyMigrated) || !self.get_config_bool(BoolKey::Fsrs)
        {
            return Ok(());
        }
        let changed: HashMap<DeckConfigId, DeckConfig> = self
            .storage
            .all_deck_config()?
            .into_iter()
            .filter(|config| legacy_fsrs_params(config) != config.fsrs_params())
            .map(|config| (config.id, config))
            .collect();
        let entries = self.memory_state_entries_for_presets(&changed, false)?;
        if !entries.is_empty() {
            self.transact_no_undo(|col| col.update_memory_state(entries))?;
        }
        self.transact_no_undo(|col| {
            col.set_config_bool_inner(BoolKey::Fsrs7OnlyMigrated, true)?;
            Ok(())
        })
    }

    /// One memory-state update per given preset that has decks, over the
    /// cards whose home deck uses it, with the preset's FSRS-7 parameters.
    pub(crate) fn memory_state_entries_for_presets(
        &self,
        configs: &HashMap<DeckConfigId, DeckConfig>,
        reschedule: bool,
    ) -> Result<Vec<UpdateMemoryStateEntry>> {
        let mut decks_by_config: HashMap<DeckConfigId, Vec<DeckId>> = HashMap::new();
        let mut deck_desired_retention = HashMap::new();
        for deck in self.storage.get_all_decks()? {
            let Ok(normal) = deck.normal() else {
                continue;
            };
            let config_id = DeckConfigId(normal.config_id);
            if configs.contains_key(&config_id) {
                decks_by_config.entry(config_id).or_default().push(deck.id);
            }
            if let Some(desired_retention) = normal.desired_retention {
                deck_desired_retention.insert(deck.id, desired_retention);
            }
        }
        let review_fuzz_config = self.review_fuzz_config();
        let total_presets = decks_by_config.len() as u32;
        decks_by_config
            .into_iter()
            .enumerate()
            .map(|(idx, (config_id, deck_ids))| {
                let config = &configs[&config_id];
                Ok(UpdateMemoryStateEntry {
                    req: Some(UpdateMemoryStateRequest {
                        params: config.fsrs_params().to_vec(),
                        preset_desired_retention: config.inner.desired_retention,
                        max_interval: config.inner.maximum_review_interval,
                        review_fuzz_config,
                        reschedule,
                        historical_retention: HISTORICAL_RETENTION,
                        deck_desired_retention: deck_desired_retention.clone(),
                        // the FSRS-7 migration, a switch to FSRS-7 and its
                        // reschedule: no stored S90 is RWKV-Curve's to keep
                        keep_stability: false,
                    }),
                    search: Node::Search(SearchNode::DeckIdsWithoutChildren(comma_separated_ids(
                        &deck_ids,
                    ))),
                    ignore_before: ignore_revlogs_before_ms_from_config(config)?,
                    preset_name: config.name.clone(),
                    current_preset: idx as u32 + 1,
                    total_presets,
                })
            })
            .collect()
    }
}

/// The parameters a preset ran with before Clanki became FSRS-7 only: the
/// slot of its stored FSRS version, else the first usable slot, else none
/// (the FSRS-6 defaults). Kept only to decide which presets
/// migrate_to_fsrs7_only must recompute.
fn legacy_fsrs_params(config: &DeckConfig) -> &[f32] {
    let usable = |params: &[f32]| {
        matches!(params.len(), 17 | 19 | 21 | 34) && params.iter().all(|w| w.is_finite())
    };
    let inner = &config.inner;
    let selected: &[f32] =
        match FsrsVersion::try_from(inner.fsrs_version).unwrap_or(FsrsVersion::Seven) {
            FsrsVersion::Seven => &inner.fsrs_params_7,
            FsrsVersion::Six => &inner.fsrs_params_6,
            FsrsVersion::Five => &inner.fsrs_params_5,
            FsrsVersion::Four => &inner.fsrs_params_4,
        };
    [
        selected,
        &inner.fsrs_params_7,
        &inner.fsrs_params_6,
        &inner.fsrs_params_5,
        &inner.fsrs_params_4,
    ]
    .into_iter()
    .find(|params| usable(params))
    .unwrap_or(&[])
}

impl Collection {
    /// Information required for the deck options screen.
    pub fn get_deck_configs_for_update(
        &mut self,
        deck: DeckId,
    ) -> Result<anki_proto::deck_config::DeckConfigsForUpdate> {
        let mut defaults = DeckConfig::default();
        defaults.inner.fsrs_params_7 = DEFAULT_PARAMETERS.into();
        defaults.inner.fsrs_version = FsrsVersion::Seven as i32;
        // Add preset and Restore defaults take the collection's algorithm
        // (spec sched.one-global-algorithm)
        self.apply_scheduling_algorithm(&mut defaults.inner);
        let last_optimize = self.get_config_i32(I32ConfigKey::LastFsrsOptimize) as u32;
        let days_since_last_fsrs_optimize = if last_optimize > 0 {
            self.timing_today()?
                .days_elapsed
                .saturating_sub(last_optimize)
        } else {
            0
        };
        let review_fuzz_config = self.stored_review_fuzz_config();
        Ok(anki_proto::deck_config::DeckConfigsForUpdate {
            review_fuzz_enabled: review_fuzz_config.enabled,
            review_fuzz_base: review_fuzz_config.base,
            review_fuzz_factor_short: review_fuzz_config.factor_short,
            review_fuzz_factor_mid: review_fuzz_config.factor_mid,
            review_fuzz_factor_long: review_fuzz_config.factor_long,
            all_config: self.get_deck_config_with_extra_for_update()?,
            current_deck: Some(self.get_current_deck_for_update(deck)?),
            defaults: Some(defaults.into()),
            schema_modified: self
                .storage
                .get_collection_timestamps()?
                .schema_changed_since_sync(),
            card_state_customizer: self.get_config_string(StringKey::CardStateCustomizer),
            new_cards_ignore_review_limit: self.get_config_bool(BoolKey::NewCardsIgnoreReviewLimit),
            apply_all_parent_limits: self.get_config_bool(BoolKey::ApplyAllParentLimits),
            fsrs: self.get_config_bool(BoolKey::Fsrs),
            load_balancer_enabled: self.get_config_bool(BoolKey::LoadBalancerEnabled),
            fsrs_short_term_with_steps_enabled: self
                .get_config_bool(BoolKey::FsrsShortTermWithStepsEnabled),
            fsrs_health_check: self.get_config_bool(BoolKey::FsrsHealthCheck),
            fsrs_legacy_evaluate: self.get_config_bool(BoolKey::FsrsLegacyEvaluate),
            days_since_last_fsrs_optimize,
            advanced_ui: self.get_config_bool(BoolKey::AdvancedUi),
            fsrs_reschedule: self.get_config_bool(BoolKey::FsrsReschedule),
            scheduling_algorithm: SchedulingAlgorithmProto::from(
                self.effective_scheduling_algorithm()?,
            ) as i32,
            recall_wording: self.recall_wording() as i32,
        })
    }

    /// Information required for the deck options screen.
    pub fn update_deck_configs(&mut self, input: UpdateDeckConfigsRequest) -> Result<OpOutput<()>> {
        self.update_deck_configs_and_algorithm(input, None)
    }

    /// A deck-options save that can also change the collection's algorithm
    /// (spec sched.one-global-algorithm). The new algorithm is set first, so
    /// the saved presets take it.
    pub fn update_deck_configs_and_algorithm(
        &mut self,
        input: UpdateDeckConfigsRequest,
        algorithm: Option<SchedulingAlgorithm>,
    ) -> Result<OpOutput<()>> {
        self.transact(Op::UpdateDeckConfig, |col| {
            if let Some(algorithm) = algorithm {
                if algorithm != col.effective_scheduling_algorithm()? {
                    col.change_scheduling_algorithm(algorithm)?;
                }
            }
            col.update_deck_configs_inner(input)
        })
    }
}

impl Collection {
    fn get_deck_config_with_extra_for_update(&self) -> Result<Vec<ConfigWithExtra>> {
        // grab the config and sort it
        let mut config = self.storage.all_deck_config()?;
        config.sort_unstable_by(|a, b| a.name.cmp(&b.name));
        // combine with use counts
        let counts = self.get_deck_config_use_counts()?;
        Ok(config
            .into_iter()
            .map(|config| ConfigWithExtra {
                use_count: counts.get(&config.id).cloned().unwrap_or_default() as u32,
                config: Some(config.into()),
            })
            .collect())
    }

    fn get_deck_config_use_counts(&self) -> Result<HashMap<DeckConfigId, usize>> {
        let mut counts = HashMap::new();
        for deck in self.storage.get_all_decks()? {
            if let Ok(normal) = deck.normal() {
                *counts.entry(DeckConfigId(normal.config_id)).or_default() += 1;
            }
        }

        Ok(counts)
    }

    fn get_current_deck_for_update(&mut self, deck: DeckId) -> Result<CurrentDeck> {
        let deck = self.get_deck(deck)?.or_not_found(deck)?;
        let normal = deck.normal()?;
        let today = self.timing_today()?.days_elapsed;

        Ok(CurrentDeck {
            name: deck.human_name(),
            config_id: normal.config_id,
            parent_config_ids: self
                .parent_config_ids(&deck)?
                .into_iter()
                .map(Into::into)
                .collect(),
            subtree_config_ids: self
                .subtree_config_ids(&deck)?
                .into_iter()
                .map(Into::into)
                .collect(),
            limits: Some(normal_deck_to_limits(normal, today)),
        })
    }

    /// Deck configs used by the selected deck and its descendants.
    fn subtree_config_ids(&self, deck: &Deck) -> Result<HashSet<DeckConfigId>> {
        Ok(self
            .storage
            .child_decks(deck)?
            .iter()
            .chain(iter::once(deck))
            .filter_map(|deck| deck.config_id())
            .collect())
    }

    /// Deck configs used by parent decks.
    fn parent_config_ids(&self, deck: &Deck) -> Result<HashSet<DeckConfigId>> {
        Ok(self
            .storage
            .parent_decks(deck)?
            .iter()
            .filter_map(|deck| {
                deck.normal()
                    .ok()
                    .map(|normal| DeckConfigId(normal.config_id))
            })
            .collect())
    }

    fn update_deck_configs_inner(&mut self, mut req: UpdateDeckConfigsRequest) -> Result<()> {
        require!(!req.configs.is_empty(), "config not provided");
        // the saved presets take the collection's algorithm (in
        // add_or_update_deck_config), and FSRS, which every algorithm needs,
        // stays on (spec sched.one-global-algorithm)
        if self.scheduling_algorithm().is_some() {
            req.fsrs = true;
        }
        let configs_before_update = self.storage.get_deck_config_map()?;
        let mut configs_after_update = configs_before_update.clone();
        let previous_review_fuzz = self.stored_review_fuzz_config();
        let review_fuzz_changed = previous_review_fuzz != req.review_fuzz_config;
        // a Preferences setting, read here rather than sent with the save
        // (spec deck-options.collection-wide-in-preferences)
        let fsrs_reschedule = self.get_config_bool(BoolKey::FsrsReschedule);
        let today = self
            .timing_today()?
            .next_day_at
            .adding_secs(-24 * 60 * 60)
            .date_string();

        // handle removals first
        for dcid in &req.removed_config_ids {
            self.remove_deck_config_inner(*dcid)?;
            configs_after_update.remove(dcid);
        }

        if req.mode == UpdateDeckConfigsMode::ComputeAllParams {
            self.compute_all_params(&mut req)?;
        }

        let config_count = req.configs.len();
        let mut save_progress = if req.mode == UpdateDeckConfigsMode::ComputeAllParams {
            let mut progress = self.new_progress_handler::<ComputeMemoryProgress>();
            progress.set(ComputeMemoryProgress {
                current_cards: 0,
                total_cards: config_count as u32,
                current_preset: 0,
                total_presets: config_count as u32,
                saving: true,
                presets: req
                    .configs
                    .iter()
                    .map(|config| ComputeMemoryPresetProgress {
                        name: config.name.clone(),
                        total_cards: 1,
                        saving: true,
                        ..Default::default()
                    })
                    .collect(),
                ..Default::default()
            })?;
            Some(progress)
        } else {
            None
        };

        // add/update provided configs
        for (idx, conf) in req.configs.iter_mut().enumerate() {
            if conf.inner.ignore_revlogs_before_date > today {
                today.clone_into(&mut conf.inner.ignore_revlogs_before_date);
            }

            // check the provided parameters are valid before we save them
            FSRS::new(conf.fsrs_params())?;
            self.add_or_update_deck_config(conf)?;
            configs_after_update.insert(conf.id, conf.clone());
            if let Some(progress) = &mut save_progress {
                progress.update(false, |state| {
                    state.current_cards = idx as u32 + 1;
                    state.total_cards = config_count as u32;
                    state.current_preset = idx as u32 + 1;
                    state.total_presets = config_count as u32;
                    state.preset_name.clone_from(&conf.name);
                    state.saving = true;
                    if let Some(preset) = state.presets.get_mut(idx) {
                        preset.current_cards = 1;
                        preset.total_cards = 1;
                        preset.finished = true;
                        preset.saving = true;
                    }
                })?;
            }
        }

        // get selected deck and possibly children
        let selected_deck_ids: HashSet<_> = if req.mode == UpdateDeckConfigsMode::ApplyToChildren {
            let deck = self
                .storage
                .get_deck(req.target_deck_id)?
                .or_not_found(req.target_deck_id)?;
            self.storage
                .child_decks(&deck)?
                .iter()
                .chain(iter::once(&deck))
                .map(|d| d.id)
                .collect()
        } else {
            [req.target_deck_id].iter().cloned().collect()
        };

        // loop through all normal decks
        let usn = self.usn()?;
        let today = self.timing_today()?.days_elapsed;
        let selected_config = req.configs.last().unwrap();
        let mut presets_with_new_fsrs_params: HashSet<DeckConfigId> = HashSet::new();
        let mut decks_needing_memory_recompute: HashMap<DeckConfigId, Vec<DeckId>> =
            Default::default();
        let fsrs_toggled = self.get_config_bool(BoolKey::Fsrs) != req.fsrs;
        if fsrs_toggled {
            self.set_config_bool_inner(BoolKey::Fsrs, req.fsrs)?;
        }
        if review_fuzz_changed {
            self.set_stored_review_fuzz_config(req.review_fuzz_config)?;
        }
        let mut deck_desired_retention: HashMap<DeckId, f32> = Default::default();
        for deck in self.storage.get_all_decks()? {
            if let Ok(normal) = deck.normal() {
                let deck_id = deck.id;
                // previous order & params
                let previous_config_id = DeckConfigId(normal.config_id);
                let previous_config = configs_before_update.get(&previous_config_id);
                let previous_order = previous_config
                    .map(|c| c.inner.new_card_insert_order())
                    .unwrap_or_default();
                let previous_params = previous_config.map(|c| c.fsrs_params());
                let previous_preset_dr = previous_config.map(|c| c.inner.desired_retention);
                let previous_deck_dr = normal.desired_retention;
                let previous_dr = previous_deck_dr.or(previous_preset_dr);
                let previous_easy_days = previous_config.map(|c| &c.inner.easy_days_percentages);

                // if a selected (sub)deck, or its old config was removed,
                // update deck to point to new config
                let (current_config_id, current_deck_dr) = if selected_deck_ids.contains(&deck.id)
                    || !configs_after_update.contains_key(&previous_config_id)
                {
                    let mut updated = deck.clone();
                    updated.normal_mut()?.config_id = selected_config.id.0;
                    update_deck_limits(updated.normal_mut()?, &req.limits, today);
                    self.update_deck_inner(&mut updated, deck, usn)?;
                    (selected_config.id, updated.normal()?.desired_retention)
                } else {
                    (previous_config_id, previous_deck_dr)
                };

                // if new order differs, deck needs re-sorting
                let current_config = configs_after_update.get(&current_config_id);
                let current_order = current_config
                    .map(|c| c.inner.new_card_insert_order())
                    .unwrap_or_default();
                if previous_order != current_order {
                    self.sort_deck(deck_id, current_order, usn)?;
                }

                // if params differ, memory state needs to be recomputed
                let current_params = current_config.map(|c| c.fsrs_params());
                let current_preset_dr = current_config.map(|c| c.inner.desired_retention);
                let current_dr = current_deck_dr.or(current_preset_dr);
                let current_easy_days = current_config.map(|c| &c.inner.easy_days_percentages);
                if fsrs_toggled
                    || previous_params != current_params
                    || previous_dr != current_dr
                    || (fsrs_reschedule && previous_easy_days != current_easy_days)
                    || (fsrs_reschedule && review_fuzz_changed)
                {
                    decks_needing_memory_recompute
                        .entry(current_config_id)
                        .or_default()
                        .push(deck_id);
                }
                // The stored per-review predictions are FSRS-7's output for
                // these parameters, so new parameters make every one of
                // them wrong (spec ui.stats-fsrs-predictions-ready). Only a
                // parameter change matters here: desired retention, easy
                // days and fuzz move the schedule, not the prediction.
                if fsrs_toggled || previous_params != current_params {
                    presets_with_new_fsrs_params.insert(current_config_id);
                }
                if let Some(desired_retention) = current_deck_dr {
                    deck_desired_retention.insert(deck_id, desired_retention);
                }
                self.adjust_remaining_steps_in_deck(deck_id, previous_config, current_config, usn)?;
            }
        }

        // Drop the superseded rows inside the same transaction as the
        // parameter change, so no graph can draw a value that the current
        // parameters did not produce. The pass that writes the new rows
        // runs in the background afterwards.
        if !presets_with_new_fsrs_params.is_empty() {
            let presets: Vec<DeckConfigId> = presets_with_new_fsrs_params.iter().copied().collect();
            self.clear_fsrs_review_predictions_of_presets(&presets)?;
        }

        if !decks_needing_memory_recompute.is_empty() {
            let total_presets = decks_needing_memory_recompute.len() as u32;
            let input: Vec<UpdateMemoryStateEntry> = decks_needing_memory_recompute
                .into_iter()
                .enumerate()
                .map(|(idx, (conf_id, search))| {
                    let config = configs_after_update.get(&conf_id);
                    let params = config.and_then(|c| {
                        if req.fsrs {
                            Some(UpdateMemoryStateRequest {
                                params: c.fsrs_params().to_vec(),
                                preset_desired_retention: c.inner.desired_retention,
                                max_interval: c.inner.maximum_review_interval,
                                review_fuzz_config: req.review_fuzz_config.review_fuzz_config(),
                                reschedule: fsrs_reschedule_for_preset(fsrs_reschedule, c),
                                historical_retention: HISTORICAL_RETENTION,
                                deck_desired_retention: deck_desired_retention.clone(),
                                keep_stability: c.inner.rwkv_review_enabled,
                            })
                        } else {
                            None
                        }
                    });
                    Ok(UpdateMemoryStateEntry {
                        req: params,
                        search: Node::Search(SearchNode::DeckIdsWithoutChildren(
                            comma_separated_ids(&search),
                        )),
                        ignore_before: config
                            .map(ignore_revlogs_before_ms_from_config)
                            .unwrap_or(Ok(0.into()))?,
                        preset_name: config
                            .map(|config| config.name.clone())
                            .unwrap_or_else(|| "Preset".to_string()),
                        current_preset: idx as u32 + 1,
                        total_presets,
                    })
                })
                .collect::<Result<_>>()?;
            self.update_memory_state(input)?;
        }

        // Limits start from top, Skip learning/relearning queues, the
        // reschedule choice, Custom scheduling and the health-check flag are
        // not written here: the first four are Preferences settings (spec
        // deck-options.collection-wide-in-preferences) and the last has no
        // control any more.
        self.set_config_bool_inner(
            BoolKey::NewCardsIgnoreReviewLimit,
            req.new_cards_ignore_review_limit,
        )?;
        self.set_config_bool_inner(BoolKey::LoadBalancerEnabled, req.load_balancer_enabled)?;
        self.set_config_bool_inner(
            BoolKey::FsrsShortTermWithStepsEnabled,
            req.fsrs_short_term_with_steps_enabled,
        )?;

        Ok(())
    }

    /// Adjust the remaining steps of cards in the given deck according to the
    /// config change.
    pub(crate) fn adjust_remaining_steps_in_deck(
        &mut self,
        deck: DeckId,
        previous_config: Option<&DeckConfig>,
        current_config: Option<&DeckConfig>,
        usn: Usn,
    ) -> Result<()> {
        if let (Some(old), Some(new)) = (previous_config, current_config) {
            for (search, old_steps, new_steps) in [
                (
                    SearchBuilder::learning_cards(),
                    &old.inner.learn_steps,
                    &new.inner.learn_steps,
                ),
                (
                    SearchBuilder::relearning_cards(),
                    &old.inner.relearn_steps,
                    &new.inner.relearn_steps,
                ),
            ] {
                if old_steps == new_steps {
                    continue;
                }
                let search = search.clone().and(SearchNode::from_deck_id(deck, false));
                for mut card in self.all_cards_for_search(search)? {
                    self.adjust_remaining_steps(&mut card, old_steps, new_steps, usn)?;
                }
            }
        }
        Ok(())
    }
    fn compute_all_params(&mut self, req: &mut UpdateDeckConfigsRequest) -> Result<()> {
        require!(req.fsrs, "FSRS must be enabled");

        // frontend didn't include any unmodified deck configs, so we need to
        // fill them in
        let changed_configs: HashSet<_> = req.configs.iter().map(|c| c.id).collect();
        let previous_last = req.configs.pop().or_invalid("no configs provided")?;
        for config in self.storage.all_deck_config()? {
            if !changed_configs.contains(&config.id) {
                req.configs.push(config);
            }
        }
        // other parts of the code expect the currently-selected preset to come
        // last
        req.configs.push(previous_last);

        // calculate and apply params to each preset
        let mut jobs = Vec::with_capacity(req.configs.len());
        for (idx, config) in req.configs.iter().enumerate() {
            let search = if config.inner.param_search.trim().is_empty() {
                SearchNode::Preset(config.name.clone())
                    .and(SearchNode::State(StateKind::Suspended).negated())
                    .try_into_search()?
                    .to_string()
            } else {
                config.inner.param_search.clone()
            };
            let ignore_revlogs_before_ms = ignore_revlogs_before_ms_from_config(config)?;
            let num_of_relearning_steps = config.inner.relearn_steps.len();
            let current_params = config.fsrs_params().to_vec();
            let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
                search: &search,
                ignore_revlogs_before: ignore_revlogs_before_ms,
                current_params: &current_params,
                num_of_relearning_steps,
                enable_scheduling_penalties: fsrs7_enable_scheduling_penalties(config),
            })?;
            if prepared.target_counts.total_targets == 0 {
                debug!(preset = config.name, "skipping FSRS preset with no reviews");
            }
            jobs.push(ComputeParamsBatchInput {
                index: idx,
                name: config.name.clone(),
                prepared,
            });
        }

        for output in self.compute_params_batch(jobs)? {
            match output.result {
                Ok(params) => {
                    if params.fsrs_items == 0 {
                        continue;
                    }
                    debug!(preset = output.name, params = ?params.params, "optimized FSRS preset");
                    req.configs[output.index].inner.fsrs_params_7 = params.params;
                }
                Err(AnkiError::Interrupted) => return Err(AnkiError::Interrupted),
                Err(err) => {
                    warn!(preset = output.name, error = %err, "failed to optimize FSRS preset");
                }
            }
        }
        let today = self.timing_today()?.days_elapsed as i32;
        self.set_config_i32_inner(I32ConfigKey::LastFsrsOptimize, today)?;
        // a preset optimized by hand is not due again for its full N days
        // (spec deck-options.fsrs-auto-optimize)
        for config in &mut req.configs {
            config.inner.fsrs_last_optimized_day = Some(today as u32);
        }
        Ok(())
    }
}

/// FSRS-7 optimization always uses scheduling penalties. The stored
/// `fsrs7EnableSchedulingPenalties` flag is ignored (spec
/// deck-options.fsrs-only-controls).
fn fsrs7_enable_scheduling_penalties(_config: &DeckConfig) -> bool {
    true
}

/// "Reschedule cards on change" applies FSRS intervals to a preset's cards
/// unless the preset schedules with RWKV-Curve, whose intervals must not be
/// overwritten; the desktop runs the RWKV-Curve reschedule after the save
/// instead (spec deck-options.reschedule-on-change).
fn fsrs_reschedule_for_preset(fsrs_reschedule: bool, config: &DeckConfig) -> bool {
    fsrs_reschedule && !config.inner.rwkv_review_enabled
}

fn normal_deck_to_limits(deck: &NormalDeck, today: u32) -> Limits {
    Limits {
        review: deck.review_limit,
        new: deck.new_limit,
        review_today: deck.review_limit_today.map(|limit| limit.limit),
        new_today: deck.new_limit_today.map(|limit| limit.limit),
        review_today_active: deck
            .review_limit_today
            .map(|limit| limit.today == today)
            .unwrap_or_default(),
        new_today_active: deck
            .new_limit_today
            .map(|limit| limit.today == today)
            .unwrap_or_default(),
        desired_retention: deck.desired_retention,
    }
}

fn update_deck_limits(deck: &mut NormalDeck, limits: &Limits, today: u32) {
    deck.review_limit = limits.review;
    deck.new_limit = limits.new;
    update_day_limit(&mut deck.review_limit_today, limits.review_today, today);
    update_day_limit(&mut deck.new_limit_today, limits.new_today, today);
    deck.desired_retention = limits.desired_retention;
}

fn update_day_limit(day_limit: &mut Option<DayLimit>, new_limit: Option<u32>, today: u32) {
    if let Some(limit) = new_limit {
        day_limit.replace(DayLimit { limit, today });
    } else {
        // if the collection was created today, the
        // "preserve last value" hack below won't work
        // clear "future" limits as well (from imports)
        day_limit.take_if(|limit| limit.today == 0 || limit.today > today);
        if let Some(limit) = day_limit {
            // instead of setting to None, only make sure today is in the past,
            // thus preserving last used value
            limit.today = limit.today.min(today.saturating_sub(1));
        }
    }
}

#[cfg(test)]
mod test {
    use fsrs::FSRS6_DEFAULT_PARAMETERS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::NewCardInsertOrder;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::memory_state::fsrs_item_for_memory_state;
    use crate::tests::open_test_collection_with_learning_card;
    use crate::tests::open_test_collection_with_relearning_card;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;
    use crate::timestamp::TimestampSecs;

    /// A review card in `deck` with three real reviews, whose memory state
    /// was computed with `params` (as the build that ran them did).
    fn reviewed_card_with_memory_state(
        col: &mut Collection,
        deck: DeckId,
        params: &[f32],
    ) -> Result<Card> {
        let note = NoteAdder::basic(col).deck(deck).add(col);
        let mut card = col.storage.all_cards_of_note(note.id)?.pop().unwrap();
        let now = TimestampMillis::now().0;
        for (days_ago, review_kind, interval) in [
            (40, RevlogReviewKind::Learning, 0),
            (30, RevlogReviewKind::Review, 10),
            (15, RevlogReviewKind::Review, 20),
        ] {
            col.storage.add_revlog_entry(
                &RevlogEntry {
                    id: RevlogId(now - days_ago * 86_400_000),
                    cid: card.id,
                    button_chosen: 3,
                    review_kind,
                    interval,
                    ease_factor: 2500,
                    ..Default::default()
                },
                false,
            )?;
        }
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 20;
        card.due = col.timing_today()?.days_elapsed as i32 + 5;
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-15 * 86_400));
        let fsrs = FSRS::new(params)?;
        let item = fsrs_item_for_memory_state(
            &fsrs,
            params,
            col.storage.get_revlog_entries_for_card(card.id)?,
            HISTORICAL_RETENTION,
            0.into(),
        )?;
        card.set_memory_state(&fsrs, params, item, HISTORICAL_RETENTION)?;
        col.storage.update_card(&card)?;
        Ok(card)
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: once, when a collection opens,
    // the cards of presets that ran other parameters get their memory states
    // computed again with the FSRS-7 parameters they now run with. Due dates
    /// Timing only, not run by default: copy a real collection to a temp
    /// folder and time the one-time FSRS-7 migration on it. Set
    /// CLANKI_TIME_MIGRATION to the collection file.
    #[test]
    #[ignore]
    fn time_migrate_to_fsrs7_only_on_a_real_collection() -> Result<()> {
        let Ok(path) = std::env::var("CLANKI_TIME_MIGRATION") else {
            return Ok(());
        };
        let dir = tempfile::tempdir()?;
        let copy = dir.path().join("collection.anki2");
        std::fs::copy(path, &copy)?;
        let mut col = crate::collection::CollectionBuilder::new(&copy)
            .set_server(true)
            .build()?;
        let cards = col
            .storage
            .db
            .query_row("select count() from cards", [], |r| r.get::<_, i64>(0))?;
        let start = std::time::Instant::now();
        col.migrate_to_fsrs7_only()?;
        eprintln!(
            "migrate_to_fsrs7_only: {} cards, {:.1} s",
            cards,
            start.elapsed().as_secs_f64()
        );
        Ok(())
    }

    // and stored parameters stay; presets already on FSRS-7 are not touched.
    #[test]
    fn migrate_to_fsrs7_only_recomputes_memory_states_once() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool_inner(BoolKey::Fsrs, true)?;
        // a preset that ran the FSRS-6 defaults and was never optimized for
        // FSRS-7
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Six as i32;
            config.fsrs_params_6 = FSRS6_DEFAULT_PARAMETERS.to_vec();
            config.fsrs_params_7.clear();
        });
        // a preset already on FSRS-7
        let fsrs7_params = DEFAULT_PARAMETERS.map(|w| w * 1.05).to_vec();
        let fsrs7_deck = DeckAdder::new("fsrs7")
            .with_config(|config| {
                config.inner.rwkv_review_enabled = false;
                config.inner.fsrs_version = FsrsVersion::Seven as i32;
                config.inner.fsrs_params_7 = fsrs7_params.clone();
            })
            .add(&mut col);
        let fsrs6_card =
            reviewed_card_with_memory_state(&mut col, DeckId(1), &FSRS6_DEFAULT_PARAMETERS)?;
        let mut fsrs7_card =
            reviewed_card_with_memory_state(&mut col, fsrs7_deck.id, &fsrs7_params)?;
        // a dummy state, so a recompute would be visible
        fsrs7_card.memory_state = Some(FsrsMemoryState {
            stability: 42.0,
            stability_internal: 42.0,
            stability_fast: Some(21.0),
            difficulty: 4.0,
        });
        col.storage.update_card(&fsrs7_card)?;
        assert!(!col.get_config_bool(BoolKey::Fsrs7OnlyMigrated));

        col.migrate_to_fsrs7_only()?;
        assert!(col.get_config_bool(BoolKey::Fsrs7OnlyMigrated));

        // the FSRS-6 preset's card now has the FSRS-7 defaults' memory state
        let migrated = col.storage.get_card(fsrs6_card.id)?.unwrap();
        assert_ne!(migrated.memory_state, fsrs6_card.memory_state);
        let mut fresh = migrated.clone();
        fresh.memory_state = None;
        col.recompute_fsrs_data_for_card(&mut fresh)?;
        assert!(fresh.memory_state.is_some());
        // storage rounds the memory state, so compare the stored forms
        col.storage.update_card(&fresh)?;
        let fresh = col.storage.get_card(fresh.id)?.unwrap();
        assert_eq!(migrated.memory_state, fresh.memory_state);
        assert_eq!(
            col.fsrs_preset_for_card(&migrated)?.params,
            DEFAULT_PARAMETERS
        );
        // not rescheduled
        assert_eq!(migrated.due, fsrs6_card.due);
        assert_eq!(migrated.interval, fsrs6_card.interval);
        // the stored parameters stay as they were
        let config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(config.inner.fsrs_version, FsrsVersion::Six as i32);
        assert_eq!(
            config.inner.fsrs_params_6,
            FSRS6_DEFAULT_PARAMETERS.to_vec()
        );
        assert!(config.inner.fsrs_params_7.is_empty());

        // the FSRS-7 preset's card is not touched
        let untouched = col.storage.get_card(fsrs7_card.id)?.unwrap();
        assert_eq!(untouched.memory_state, fsrs7_card.memory_state);
        assert_eq!(untouched.due, fsrs7_card.due);

        // the migration runs once
        let mut changed_later = migrated.clone();
        changed_later.memory_state = Some(FsrsMemoryState {
            stability: 7.0,
            stability_internal: 7.0,
            stability_fast: Some(3.0),
            difficulty: 6.0,
        });
        col.storage.update_card(&changed_later)?;
        col.migrate_to_fsrs7_only()?;
        assert_eq!(
            col.storage.get_card(fsrs6_card.id)?.unwrap().memory_state,
            changed_later.memory_state
        );
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: with FSRS off the migration
    // writes nothing, so a new collection stays unmodified (upstream issue
    // #5109: its first sync must not need a full sync).
    #[test]
    fn migrate_to_fsrs7_only_writes_nothing_while_fsrs_is_off() -> Result<()> {
        let mut col = Collection::new();
        assert!(!col.get_config_bool(BoolKey::Fsrs));
        let modified_before = col.storage.get_collection_timestamps()?.collection_change;
        col.migrate_to_fsrs7_only()?;
        assert!(!col.get_config_bool(BoolKey::Fsrs7OnlyMigrated));
        assert_eq!(
            col.storage.get_collection_timestamps()?.collection_change,
            modified_before
        );
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.fsrs-only-controls
    #[test]
    fn fsrs7_scheduling_penalties_are_always_enabled() -> Result<()> {
        let mut config = DeckConfig::default();
        config.inner.fsrs_version = FsrsVersion::Seven as i32;
        config.inner.other = serde_json::to_vec(&serde_json::json!({
            "fsrs7EnableSchedulingPenalties": false,
        }))?;
        assert!(fsrs7_enable_scheduling_penalties(&config));
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.collection-wide-in-preferences
    #[test]
    fn deck_options_save_leaves_the_collection_wide_settings_alone() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool_inner(BoolKey::FsrsReschedule, true)?;
        col.set_config_bool_inner(BoolKey::ApplyAllParentLimits, true)?;
        col.set_config_string_inner(StringKey::CardStateCustomizer, "// custom")?;
        let mut input = col.get_deck_configs_for_update(DeckId(1))?;
        // the page reads the reschedule choice for its Easy Days warning
        assert!(input.fsrs_reschedule);
        let req = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: input
                .all_config
                .drain(..)
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        col.update_deck_configs(req)?;
        assert!(col.get_config_bool(BoolKey::FsrsReschedule));
        assert!(col.get_config_bool(BoolKey::ApplyAllParentLimits));
        assert_eq!(
            col.get_config_string(StringKey::CardStateCustomizer),
            "// custom"
        );
        Ok(())
    }

    #[test]
    fn fsrs_reschedule_skips_rwkv_curve_presets() {
        let mut config = DeckConfig::default();
        config.inner.rwkv_review_enabled = false;
        assert!(fsrs_reschedule_for_preset(true, &config));
        assert!(!fsrs_reschedule_for_preset(false, &config));

        config.inner.rwkv_review_enabled = true;
        assert!(!fsrs_reschedule_for_preset(true, &config));

        config.inner.rwkv_review_enabled = false;
        config.inner.rwkv_review_instant_order_enabled = true;
        assert!(fsrs_reschedule_for_preset(true, &config));
    }

    #[test]
    fn updating() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        let card1_id = col.storage.card_ids_of_notes(&[note1.id])?[0];
        for _ in 0..9 {
            let mut note = nt.new_note();
            col.add_note(&mut note, DeckId(1))?;
        }

        // add the keys so it doesn't trigger a change below
        col.set_config_string_inner(StringKey::CardStateCustomizer, "")?;
        col.set_config_bool_inner(BoolKey::NewCardsIgnoreReviewLimit, false)?;
        col.set_config_bool_inner(BoolKey::ApplyAllParentLimits, false)?;
        col.set_config_bool_inner(BoolKey::LoadBalancerEnabled, false)?;
        col.set_config_bool_inner(BoolKey::FsrsShortTermWithStepsEnabled, false)?;
        col.set_config_bool_inner(BoolKey::FsrsHealthCheck, true)?;

        // pretend we're in sync
        let stamps = col.storage.get_collection_timestamps()?;
        col.storage.set_last_sync(stamps.schema_change)?;

        let full_sync_required = |col: &mut Collection| -> bool {
            col.storage
                .get_collection_timestamps()
                .unwrap()
                .schema_changed_since_sync()
        };
        let reset_card1_pos = |col: &mut Collection| {
            let mut card = col.storage.get_card(card1_id).unwrap().unwrap();
            // set it out of bounds, so we can be sure it has changed
            card.due = 0;
            col.storage.update_card(&card).unwrap();
        };
        let card1_pos = |col: &mut Collection| col.storage.get_card(card1_id).unwrap().unwrap().due;

        // if nothing changed, no changes should be made
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };
        assert!(!col.update_deck_configs(input.clone())?.changes.had_change());

        // modifying a value should update the config, but not the deck
        input.configs[0].inner.new_per_day += 1;
        let changes = col.update_deck_configs(input.clone())?.changes.changes;
        assert!(!changes.deck);
        assert!(changes.deck_config);
        assert!(!changes.card);

        // adding a new config will update the deck as well
        let new_config = DeckConfig {
            id: DeckConfigId(0),
            ..input.configs[0].clone()
        };
        input.configs.push(new_config);
        let changes = col.update_deck_configs(input.clone())?.changes.changes;
        assert!(changes.deck);
        assert!(changes.deck_config);
        assert!(!changes.card);
        let allocated_id = col.get_deck(DeckId(1))?.unwrap().normal()?.config_id;
        assert_ne!(allocated_id, 0);
        assert_ne!(allocated_id, 1);

        // changing the order will cause the cards to be re-sorted
        assert_eq!(card1_pos(&mut col), 1);
        reset_card1_pos(&mut col);
        assert_eq!(card1_pos(&mut col), 0);
        input.configs[1].inner.new_card_insert_order = NewCardInsertOrder::Random as i32;
        assert!(col.update_deck_configs(input.clone())?.changes.changes.card);
        assert_ne!(card1_pos(&mut col), 0);

        // removing the config will assign the selected config (default in this
        // case), and as default has normal sort order, that will reset
        // the order again
        assert!(!full_sync_required(&mut col));
        reset_card1_pos(&mut col);
        input.configs.remove(1);
        input.removed_config_ids.push(DeckConfigId(allocated_id));
        col.update_deck_configs(input)?;
        let current_id = col.get_deck(DeckId(1))?.unwrap().normal()?.config_id;
        assert_eq!(current_id, 1);
        assert_eq!(card1_pos(&mut col), 1);
        // should have forced a full sync
        assert!(full_sync_required(&mut col));

        Ok(())
    }

    #[test]
    fn advanced_ui_flag_is_reported() -> Result<()> {
        let mut col = Collection::new();
        assert!(!col.get_deck_configs_for_update(DeckId(1))?.advanced_ui);
        col.set_config_bool(BoolKey::AdvancedUi, true, false)?;
        assert!(col.get_deck_configs_for_update(DeckId(1))?.advanced_ui);
        Ok(())
    }

    #[test]
    fn current_deck_reports_subtree_config_ids() -> Result<()> {
        let mut col = Collection::new();
        let mut child_config = DeckConfig {
            name: "Child preset".into(),
            ..Default::default()
        };
        col.add_or_update_deck_config(&mut child_config)?;

        let mut child = col.get_or_create_normal_deck("Default::child")?;
        child.normal_mut()?.config_id = child_config.id.0;
        col.add_or_update_deck(&mut child)?;

        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let subtree_config_ids: HashSet<_> = output
            .current_deck
            .unwrap()
            .subtree_config_ids
            .into_iter()
            .collect();

        assert_eq!(
            subtree_config_ids,
            HashSet::from([DeckConfigId(1).0, child_config.id.0])
        );
        Ok(())
    }

    #[test]
    fn fsrs7_params_are_preserved_on_update() -> Result<()> {
        let mut col = Collection::new();
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };
        let expected = vec![0.1, 0.2, 0.3];
        input.configs[0].inner.fsrs_params_7 = expected.clone();
        col.update_deck_configs(input)?;

        let stored = col.get_deck_config(DeckConfigId(1), true)?.unwrap();
        assert_eq!(stored.inner.fsrs_params_7, expected);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-only
    #[test]
    fn fsrs6_shaped_fsrs7_params_run_the_fsrs7_defaults_on_update() -> Result<()> {
        let mut col = Collection::new();
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };
        // 21 values are FSRS-6 parameters, not FSRS-7 ones: the save keeps
        // them as stored, but the preset runs the FSRS-7 defaults.
        let fsrs6_shaped = vec![
            0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
            1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
        ];
        input.configs[0].inner.fsrs_params_6 = vec![1.0; 21];
        input.configs[0].inner.fsrs_params_7 = fsrs6_shaped.clone();
        col.update_deck_configs(input)?;

        let stored = col.get_deck_config(DeckConfigId(1), true)?.unwrap();
        assert_eq!(stored.inner.fsrs_params_7, fsrs6_shaped);
        assert_eq!(stored.inner.fsrs_params_6, vec![1.0; 21]);
        assert_eq!(stored.fsrs_params(), &DEFAULT_PARAMETERS[..]);
        Ok(())
    }

    /// A card in `deck` with one rated review, and a stored FSRS-7
    /// prediction of it, as a finished pass would have left behind.
    fn card_with_stored_prediction(
        col: &mut Collection,
        deck: DeckId,
        days_ago: i64,
    ) -> Result<RevlogId> {
        let note = NoteAdder::basic(col).deck(deck).add(col);
        let card = col.storage.all_cards_of_note(note.id)?.pop().unwrap();
        let review = RevlogId(TimestampMillis::now().0 - days_ago * 86_400_000);
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: review,
                cid: card.id,
                button_chosen: 3,
                review_kind: RevlogReviewKind::Review,
                interval: 10,
                ease_factor: 2500,
                ..Default::default()
            },
            false,
        )?;
        col.storage.set_fsrs_review_retrievability_predictions(
            &[crate::storage::FsrsReviewRetrievabilityCacheRow {
                revlog_id: review,
                prediction: 0.9,
                sample_role: crate::storage::FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: 0,
            }],
            "test",
        )?;
        Ok(review)
    }

    fn stored_predictions(col: &Collection) -> usize {
        col.storage
            .cached_review_predictions(
                crate::storage::FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
                "validation_fold",
                0.into(),
            )
            .unwrap()
            .len()
    }

    /// Turns FSRS on with one save, so that a later save in the same test
    /// changes only what it means to change: switching the algorithm on is
    /// itself a parameter change, and drops the stored predictions.
    fn settle_fsrs_on(col: &mut Collection) -> Result<()> {
        let input = save_request(col)?;
        col.update_deck_configs(input)?;
        Ok(())
    }

    fn save_request(col: &mut Collection) -> Result<UpdateDeckConfigsRequest> {
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        Ok(UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: true,
            review_fuzz_config: Default::default(),
        })
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready
    #[test]
    fn a_parameter_change_drops_that_presets_predictions() -> Result<()> {
        let mut col = Collection::new();
        settle_fsrs_on(&mut col)?;
        card_with_stored_prediction(&mut col, DeckId(1), 10)?;
        assert_eq!(stored_predictions(&col), 1);

        let mut input = save_request(&mut col)?;
        input.configs[0].inner.fsrs_params_7 =
            (0..34).map(|index| 0.1 + index as f32 * 0.01).collect();
        col.update_deck_configs(input)?;

        // the prediction was FSRS-7's output for the old parameters, so it
        // is gone rather than kept and drawn
        assert_eq!(stored_predictions(&col), 0);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready
    #[test]
    fn another_presets_predictions_survive_a_parameter_change() -> Result<()> {
        let mut col = Collection::new();
        settle_fsrs_on(&mut col)?;
        let other_deck = DeckAdder::new("other")
            .with_config(|config| config.name = "Other".to_string())
            .add(&mut col);
        card_with_stored_prediction(&mut col, DeckId(1), 10)?;
        card_with_stored_prediction(&mut col, other_deck.id, 20)?;
        assert_eq!(stored_predictions(&col), 2);

        let mut input = save_request(&mut col)?;
        // only the default preset's parameters change
        assert_eq!(input.configs.len(), 2, "both presets are in the save");
        // the save applies its LAST preset to the decks it targets, as the
        // deck-options screen does, so "Other" goes last and is the one
        // whose parameters change
        input.target_deck_id = other_deck.id;
        input
            .configs
            .sort_by_key(|config| u8::from(config.name == "Other"));
        let target = input.configs.last_mut().unwrap();
        assert_eq!(target.name, "Other");
        target.inner.fsrs_params_7 = (0..34).map(|index| 0.1 + index as f32 * 0.01).collect();
        col.update_deck_configs(input)?;

        // the preset whose parameters did not change keeps its row
        assert_eq!(stored_predictions(&col), 1);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready
    #[test]
    fn a_desired_retention_change_keeps_the_predictions() -> Result<()> {
        let mut col = Collection::new();
        settle_fsrs_on(&mut col)?;
        card_with_stored_prediction(&mut col, DeckId(1), 10)?;

        let mut input = save_request(&mut col)?;
        input.configs[0].inner.desired_retention = 0.85;
        col.update_deck_configs(input)?;

        // desired retention moves the schedule, not the prediction
        assert_eq!(stored_predictions(&col), 1);
        Ok(())
    }

    #[test]
    fn valid_34_param_fsrs7_is_preferred_on_update() -> Result<()> {
        let mut col = Collection::new();
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };
        let expected: Vec<f32> = (0..34).map(|i| 0.1 + i as f32 * 0.01).collect();
        input.configs[0].inner.fsrs_params_6 = vec![1.0; 21];
        input.configs[0].inner.fsrs_params_7 = expected.clone();
        col.update_deck_configs(input)?;

        let stored = col.get_deck_config(DeckConfigId(1), true)?.unwrap();
        assert_eq!(stored.fsrs_params(), &expected);
        Ok(())
    }

    #[test]
    fn fsrs_short_term_with_steps_flag_roundtrip() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool_inner(BoolKey::FsrsShortTermWithStepsEnabled, true)?;
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        assert!(output.fsrs_short_term_with_steps_enabled);

        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };
        col.update_deck_configs(input.clone())?;
        // the same-day flag is always on, whatever a save writes
        // (spec sched.same-day-steps-always-on)
        assert!(col.get_config_bool(BoolKey::FsrsShortTermWithStepsEnabled));

        input.fsrs_short_term_with_steps_enabled = true;
        col.update_deck_configs(input)?;
        assert!(col.get_config_bool(BoolKey::FsrsShortTermWithStepsEnabled));
        Ok(())
    }

    #[test]
    fn should_increase_remaining_learning_steps_if_unpassed_learning_step_added() {
        let mut col = open_test_collection_with_learning_card();
        col.set_default_learn_steps(vec![1., 10., 100.]);
        assert_eq!(col.get_first_card().remaining_steps, 3);
    }

    #[test]
    fn should_keep_remaining_learning_steps_if_unpassed_relearning_step_added() {
        let mut col = open_test_collection_with_learning_card();
        col.set_default_relearn_steps(vec![1., 10., 100.]);
        assert_eq!(col.get_first_card().remaining_steps, 2);
    }

    #[test]
    fn should_keep_remaining_learning_steps_if_passed_learning_step_added() {
        let mut col = open_test_collection_with_learning_card();
        col.answer_good();
        col.set_default_learn_steps(vec![1., 1., 10.]);
        assert_eq!(col.get_first_card().remaining_steps, 1);
    }

    #[test]
    fn should_keep_at_least_one_remaining_learning_step() {
        let mut col = open_test_collection_with_learning_card();
        col.answer_good();
        col.set_default_learn_steps(vec![1.]);
        assert_eq!(col.get_first_card().remaining_steps, 1);
    }

    #[test]
    fn should_increase_remaining_relearning_steps_if_unpassed_relearning_step_added() {
        let mut col = open_test_collection_with_relearning_card();
        col.set_default_relearn_steps(vec![1., 10., 100.]);
        assert_eq!(col.get_first_card().remaining_steps, 3);
    }

    #[test]
    fn should_keep_remaining_relearning_steps_if_unpassed_learning_step_added() {
        let mut col = open_test_collection_with_relearning_card();
        col.set_default_learn_steps(vec![1., 10., 100.]);
        assert_eq!(col.get_first_card().remaining_steps, 1);
    }

    #[test]
    fn should_keep_remaining_relearning_steps_if_passed_relearning_step_added() {
        let mut col = open_test_collection_with_relearning_card();
        col.set_default_relearn_steps(vec![10., 100.]);
        col.answer_good();
        col.set_default_relearn_steps(vec![1., 10., 100.]);
        assert_eq!(col.get_first_card().remaining_steps, 1);
    }

    #[test]
    fn should_keep_at_least_one_remaining_relearning_step() {
        let mut col = open_test_collection_with_relearning_card();
        col.set_default_relearn_steps(vec![10., 100.]);
        col.answer_good();
        col.set_default_relearn_steps(vec![1.]);
        assert_eq!(col.get_first_card().remaining_steps, 1);
    }

    #[test]
    fn should_clamp_ignore_revlogs_before_date_to_today() {
        let mut col = Collection::new();
        let today = col
            .timing_today()
            .unwrap()
            .next_day_at
            .adding_secs(-24 * 60 * 60)
            .date_string();
        let output = col.get_deck_configs_for_update(DeckId(1)).unwrap();
        let base_input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            fsrs: false,
            review_fuzz_config: Default::default(),
        };

        // future date should be clamped to today
        let mut future_input = base_input.clone();
        future_input.configs[0].inner.ignore_revlogs_before_date =
            TimestampSecs::now().adding_secs(86_400).date_string();
        assert!(col.update_deck_configs(future_input).is_ok());

        let updated = col.get_deck_configs_for_update(DeckId(1)).unwrap();
        let updated_config: DeckConfig = updated.all_config[0].config.clone().unwrap().into();
        assert_eq!(updated_config.inner.ignore_revlogs_before_date, today);

        // past dates should be left unchanged
        let past_date = TimestampSecs::now().adding_secs(-86_400).date_string();
        let mut past_input = base_input.clone();
        past_input.configs[0].inner.ignore_revlogs_before_date = past_date.clone();
        assert!(col.update_deck_configs(past_input).is_ok());
        let updated2 = col.get_deck_configs_for_update(DeckId(1)).unwrap();
        let updated_config2: DeckConfig = updated2.all_config[0].config.clone().unwrap().into();
        assert_eq!(updated_config2.inner.ignore_revlogs_before_date, past_date);

        // today's date should also be left unchanged
        let mut today_input = base_input.clone();
        today_input.configs[0].inner.ignore_revlogs_before_date = today.clone();
        assert!(col.update_deck_configs(today_input).is_ok());
        let updated3 = col.get_deck_configs_for_update(DeckId(1)).unwrap();
        let updated_config3: DeckConfig = updated3.all_config[0].config.clone().unwrap().into();
        assert_eq!(updated_config3.inner.ignore_revlogs_before_date, today);

        // empty dates should be saved as is and handled by
        // ignore_revlogs_before_date_to_ms
        let mut today_input = base_input;
        today_input.configs[0].inner.ignore_revlogs_before_date = "".to_string();
        assert!(col.update_deck_configs(today_input).is_ok());
        let updated3 = col.get_deck_configs_for_update(DeckId(1)).unwrap();
        let updated_config3: DeckConfig = updated3.all_config[0].config.clone().unwrap().into();
        assert_eq!(
            updated_config3.inner.ignore_revlogs_before_date,
            "".to_string()
        );
    }
}
