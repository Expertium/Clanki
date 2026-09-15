// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The collection's one scheduling algorithm (spec sched.one-global-algorithm).
//!
//! The `schedulingAlgorithm` config key is the source of truth. Every preset
//! carries it as its two RWKV flags (a mirror), so the scheduler, the Python
//! code and other clients that read the flags all see the same algorithm.

use std::collections::HashMap;

use anki_proto::config::preferences::scheduling::Algorithm as SchedulingAlgorithmProto;
use serde::Deserialize;
use serde::Serialize;

use super::DeckConfigInner;
use crate::config::ConfigKey;
use crate::prelude::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum SchedulingAlgorithm {
    Fsrs7,
    RwkvCurve,
    RwkvInstant,
}

impl SchedulingAlgorithm {
    /// The algorithm a preset's flags select. RWKV-Curve wins when both RWKV
    /// flags are on (spec deck-options.scheduler-choice).
    pub(crate) fn of_preset(inner: &DeckConfigInner) -> Self {
        if inner.rwkv_review_enabled {
            Self::RwkvCurve
        } else if inner.rwkv_review_instant_order_enabled {
            Self::RwkvInstant
        } else {
            Self::Fsrs7
        }
    }

    pub(crate) fn apply_to(self, inner: &mut DeckConfigInner) {
        inner.rwkv_review_enabled = self == Self::RwkvCurve;
        inner.rwkv_review_instant_order_enabled = self == Self::RwkvInstant;
    }

    fn is_applied_to(self, inner: &DeckConfigInner) -> bool {
        inner.rwkv_review_enabled == (self == Self::RwkvCurve)
            && inner.rwkv_review_instant_order_enabled == (self == Self::RwkvInstant)
    }
}

impl From<SchedulingAlgorithm> for SchedulingAlgorithmProto {
    fn from(algorithm: SchedulingAlgorithm) -> Self {
        match algorithm {
            SchedulingAlgorithm::Fsrs7 => Self::Fsrs7,
            SchedulingAlgorithm::RwkvCurve => Self::RwkvCurve,
            SchedulingAlgorithm::RwkvInstant => Self::RwkvInstant,
        }
    }
}

impl From<SchedulingAlgorithmProto> for SchedulingAlgorithm {
    fn from(algorithm: SchedulingAlgorithmProto) -> Self {
        match algorithm {
            SchedulingAlgorithmProto::Fsrs7 => Self::Fsrs7,
            SchedulingAlgorithmProto::RwkvCurve => Self::RwkvCurve,
            SchedulingAlgorithmProto::RwkvInstant => Self::RwkvInstant,
        }
    }
}

impl Collection {
    /// The collection's algorithm; None until the collection has one (an
    /// unknown stored value counts as none).
    pub(crate) fn scheduling_algorithm(&self) -> Option<SchedulingAlgorithm> {
        self.get_config_optional(ConfigKey::SchedulingAlgorithm)
    }

    /// The algorithm Preferences shows: the collection's, or, before it has
    /// one, the Default preset's.
    pub(crate) fn effective_scheduling_algorithm(&self) -> Result<SchedulingAlgorithm> {
        if let Some(algorithm) = self.scheduling_algorithm() {
            return Ok(algorithm);
        }
        Ok(self
            .get_deck_config(DeckConfigId(1), true)?
            .map(|config| SchedulingAlgorithm::of_preset(&config.inner))
            .unwrap_or(SchedulingAlgorithm::RwkvCurve))
    }

    /// A preset about to be written takes the collection's algorithm, so no
    /// write path (deck options, add-ons, AnkiConnect, import) can give one
    /// preset another algorithm.
    pub(crate) fn apply_scheduling_algorithm(&self, inner: &mut DeckConfigInner) {
        if let Some(algorithm) = self.scheduling_algorithm() {
            algorithm.apply_to(inner);
        }
    }

    /// Makes `algorithm` the collection's algorithm and mirrors it into every
    /// preset.
    fn set_scheduling_algorithm_inner(&mut self, algorithm: SchedulingAlgorithm) -> Result<()> {
        self.set_config(ConfigKey::SchedulingAlgorithm, &algorithm)?;
        self.mirror_scheduling_algorithm(algorithm)?;
        Ok(())
    }

    /// The user's choice in Preferences. FSRS goes on (every algorithm needs
    /// the FSRS memory states). Under FSRS-7, and whenever FSRS was off,
    /// every card's memory state is computed again from its review log, so
    /// no RWKV-Curve stability stays behind; due dates do not change (the
    /// "Reschedule all cards now" answer runs reschedule_all_cards_with_fsrs7
    /// after this).
    pub(crate) fn change_scheduling_algorithm(
        &mut self,
        algorithm: SchedulingAlgorithm,
    ) -> Result<()> {
        self.set_scheduling_algorithm_inner(algorithm)?;
        let fsrs_was_off = !self.get_config_bool(BoolKey::Fsrs);
        if fsrs_was_off {
            self.set_config_bool_inner(BoolKey::Fsrs, true)?;
        }
        if fsrs_was_off || algorithm == SchedulingAlgorithm::Fsrs7 {
            let configs = self.storage.get_deck_config_map()?;
            let entries = self.memory_state_entries_for_presets(&configs, false)?;
            self.update_memory_state(entries)?;
        }
        Ok(())
    }

    /// Gives every preset the algorithm's flags. True if any changed.
    fn mirror_scheduling_algorithm(&mut self, algorithm: SchedulingAlgorithm) -> Result<bool> {
        let usn = self.usn()?;
        let mut changed = false;
        for original in self.storage.all_deck_config()? {
            if !algorithm.is_applied_to(&original.inner) {
                let mut config = original.clone();
                algorithm.apply_to(&mut config.inner);
                self.update_deck_config_inner(&mut config, original, Some(usn))?;
                changed = true;
            }
        }
        Ok(changed)
    }

    /// Run when the collection opens, after a normal sync and after an .apkg
    /// import: a collection without an algorithm gets the one that schedules
    /// the most cards, and the presets are brought in line with it (another
    /// client may have changed one). A collection without cards and without
    /// an algorithm is left untouched. True if anything changed.
    pub(crate) fn enforce_scheduling_algorithm_inner(&mut self) -> Result<bool> {
        if let Some(algorithm) = self.scheduling_algorithm() {
            return self.mirror_scheduling_algorithm(algorithm);
        }
        let Some(algorithm) = self.most_used_scheduling_algorithm()? else {
            return Ok(false);
        };
        self.set_scheduling_algorithm_inner(algorithm)?;
        Ok(true)
    }

    /// FSRS-7 memory states and due dates for every card, when the
    /// collection switches to FSRS-7 and the user chooses to reschedule
    /// (spec sched.algorithm-change-prompt). Writes no review-log rows.
    pub fn reschedule_all_cards_with_fsrs7(&mut self) -> Result<OpOutput<()>> {
        self.transact(Op::UpdatePreferences, |col| {
            require!(
                col.effective_scheduling_algorithm()? == SchedulingAlgorithm::Fsrs7,
                "the collection does not run FSRS-7"
            );
            let configs = col.storage.get_deck_config_map()?;
            let entries = col.memory_state_entries_for_presets(&configs, true)?;
            col.update_memory_state(entries)
        })
    }

    /// At open and after a sync. A transaction marks the collection modified,
    /// so it starts only when there is something to write: a new, empty
    /// collection must not need a full sync (upstream issue #5109). True if
    /// anything changed.
    pub(crate) fn enforce_scheduling_algorithm(&mut self) -> Result<bool> {
        let work_to_do = match self.scheduling_algorithm() {
            Some(algorithm) => self
                .storage
                .all_deck_config()?
                .iter()
                .any(|config| !algorithm.is_applied_to(&config.inner)),
            None => self
                .storage
                .db
                .query_row("SELECT EXISTS(SELECT 1 FROM cards)", [], |row| row.get(0))?,
        };
        if !work_to_do {
            return Ok(false);
        }
        self.transact_no_undo(|col| col.enforce_scheduling_algorithm_inner())
    }

    /// The algorithm of the presets that schedule the most review and
    /// relearning cards, counted by each card's home deck; with no such
    /// cards, of the most cards. Ties go to RWKV-Curve, then FSRS-7. None
    /// for a collection without cards (spec sched.global-algorithm-migration).
    fn most_used_scheduling_algorithm(&self) -> Result<Option<SchedulingAlgorithm>> {
        let presets = self.storage.get_deck_config_map()?;
        let default_preset = presets
            .get(&DeckConfigId(1))
            .map(|config| SchedulingAlgorithm::of_preset(&config.inner))
            .unwrap_or(SchedulingAlgorithm::RwkvCurve);
        let mut deck_algorithms = HashMap::new();
        for deck in self.storage.get_all_decks()? {
            if let Ok(normal) = deck.normal() {
                let algorithm = presets
                    .get(&DeckConfigId(normal.config_id))
                    .map(|config| SchedulingAlgorithm::of_preset(&config.inner))
                    .unwrap_or(default_preset);
                deck_algorithms.insert(deck.id, algorithm);
            }
        }
        let mut reviewed: HashMap<SchedulingAlgorithm, u64> = HashMap::new();
        let mut all: HashMap<SchedulingAlgorithm, u64> = HashMap::new();
        let mut stmt = self.storage.db.prepare(
            "SELECT CASE WHEN odid = 0 THEN did ELSE odid END, type IN (2, 3), count(*)
             FROM cards GROUP BY 1, 2",
        )?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let deck_id = DeckId(row.get(0)?);
            let is_review: bool = row.get(1)?;
            let count: u64 = row.get(2)?;
            let algorithm = deck_algorithms
                .get(&deck_id)
                .copied()
                .unwrap_or(default_preset);
            *all.entry(algorithm).or_default() += count;
            if is_review {
                *reviewed.entry(algorithm).or_default() += count;
            }
        }
        let counts = if reviewed.is_empty() { all } else { reviewed };
        Ok([
            SchedulingAlgorithm::RwkvCurve,
            SchedulingAlgorithm::Fsrs7,
            SchedulingAlgorithm::RwkvInstant,
        ]
        .into_iter()
        .filter_map(|algorithm| counts.get(&algorithm).map(|&count| (algorithm, count)))
        // max_by_key returns the last of equal maxima: reversed, ties go to
        // the earlier algorithm in the list above
        .rev()
        .max_by_key(|&(_, count)| count)
        .map(|(algorithm, _)| algorithm))
    }
}

#[cfg(test)]
mod test {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;

    use super::SchedulingAlgorithm::*;
    use super::*;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;

    fn add_deck(col: &mut Collection, name: &str, algorithm: SchedulingAlgorithm) -> DeckId {
        DeckAdder::new(name)
            .with_config(|config| algorithm.apply_to(&mut config.inner))
            .add(col)
            .id
    }

    fn add_cards(col: &mut Collection, deck: DeckId, count: usize, review: bool) {
        for _ in 0..count {
            let note = NoteAdder::basic(col).deck(deck).add(col);
            if review {
                col.storage
                    .db
                    .execute(
                        "update cards set type = 2, queue = 2 where nid = ?",
                        [note.id],
                    )
                    .unwrap();
            }
        }
    }

    fn preset_algorithms(col: &Collection) -> Vec<SchedulingAlgorithm> {
        col.storage
            .all_deck_config()
            .unwrap()
            .iter()
            .map(|config| SchedulingAlgorithm::of_preset(&config.inner))
            .collect()
    }

    // Pins spec/scheduling.md#sched.global-algorithm-migration
    #[test]
    fn a_collection_without_an_algorithm_gets_the_one_with_most_review_cards() -> Result<()> {
        let mut col = Collection::new();
        let curve = add_deck(&mut col, "curve", RwkvCurve);
        let instant = add_deck(&mut col, "instant", RwkvInstant);
        // more cards under FSRS-7, but new ones
        add_cards(&mut col, DeckId(1), 5, false);
        add_cards(&mut col, curve, 1, true);
        add_cards(&mut col, instant, 2, true);
        assert_eq!(col.scheduling_algorithm(), None);

        col.enforce_scheduling_algorithm()?;

        assert_eq!(col.scheduling_algorithm(), Some(RwkvInstant));
        assert_eq!(preset_algorithms(&col), vec![RwkvInstant; 3]);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.global-algorithm-migration
    #[test]
    fn migration_counts_filtered_cards_by_home_deck_and_breaks_ties() -> Result<()> {
        let mut col = Collection::new();
        let curve = add_deck(&mut col, "curve", RwkvCurve);
        let filtered = DeckAdder::new("filtered").filtered(true).add(&mut col).id;
        add_cards(&mut col, DeckId(1), 2, true);
        add_cards(&mut col, curve, 1, true);
        // a card of the RWKV-Curve deck, now in a filtered deck
        let note = NoteAdder::basic(&mut col).deck(curve).add(&mut col);
        col.storage.db.execute(
            "update cards set type = 2, queue = 2, odid = did, did = ? where nid = ?",
            [filtered.0, note.id.0],
        )?;
        // 2 against 2: the tie goes to RWKV-Curve
        assert_eq!(col.most_used_scheduling_algorithm()?, Some(RwkvCurve));

        // without review cards, all cards count
        col.storage
            .db
            .execute("update cards set type = 0, queue = 0", [])?;
        add_cards(&mut col, DeckId(1), 1, false);
        assert_eq!(col.most_used_scheduling_algorithm()?, Some(Fsrs7));
        Ok(())
    }

    // Pins spec/scheduling.md#sched.global-algorithm-migration
    #[test]
    fn a_collection_without_cards_gets_no_algorithm() -> Result<()> {
        let mut col = Collection::new();
        let before = col.storage.get_collection_timestamps()?.collection_change;
        col.enforce_scheduling_algorithm()?;
        assert_eq!(col.scheduling_algorithm(), None);
        assert_eq!(
            col.storage.get_collection_timestamps()?.collection_change,
            before
        );
        Ok(())
    }

    // Pins spec/scheduling.md#sched.one-global-algorithm
    #[test]
    fn every_preset_write_takes_the_collection_algorithm() -> Result<()> {
        let mut col = Collection::new();
        col.change_scheduling_algorithm(RwkvCurve)?;
        assert_eq!(preset_algorithms(&col), vec![RwkvCurve]);
        assert!(col.get_config_bool(BoolKey::Fsrs));

        // a new preset
        add_deck(&mut col, "fsrs", Fsrs7);
        // an updated preset
        col.update_default_deck_config(|inner| RwkvInstant.apply_to(inner));
        // the legacy path (old import code, add-ons)
        let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        Fsrs7.apply_to(&mut config.inner);
        col.add_or_update_deck_config_legacy(&mut config)?;
        assert_eq!(preset_algorithms(&col), vec![RwkvCurve; 2]);

        // Add preset and Restore defaults in deck options
        let defaults = col
            .get_deck_configs_for_update(DeckId(1))?
            .defaults
            .unwrap()
            .config
            .unwrap();
        assert!(defaults.rwkv_review_enabled && !defaults.rwkv_review_instant_order_enabled);

        // another client changed a preset: the next open (or sync) reverts it
        let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        RwkvInstant.apply_to(&mut config.inner);
        col.storage.update_deck_conf(&config)?;
        assert!(col.enforce_scheduling_algorithm_inner()?);
        assert_eq!(preset_algorithms(&col), vec![RwkvCurve; 2]);
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.scheduler-choice
    #[test]
    fn deck_options_save_cannot_change_the_algorithm() -> Result<()> {
        let mut col = Collection::new();
        col.change_scheduling_algorithm(RwkvCurve)?;
        let mut config = col.get_deck_config(DeckConfigId(1), false)?.unwrap();
        Fsrs7.apply_to(&mut config.inner);
        col.update_deck_configs(UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: vec![config],
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: false,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: col.stored_review_fuzz_config(),
        })?;
        assert_eq!(preset_algorithms(&col), vec![RwkvCurve]);
        assert!(col.get_config_bool(BoolKey::Fsrs));
        Ok(())
    }

    // Pins spec/scheduling.md#sched.one-global-algorithm
    #[test]
    fn scheduling_preferences_carry_the_algorithm() -> Result<()> {
        let mut col = Collection::new();
        let mut prefs = col.get_scheduling_preferences()?;
        // before the collection has one: the Default preset's
        assert_eq!(
            prefs.algorithm,
            Some(SchedulingAlgorithmProto::Fsrs7 as i32)
        );

        // an unchanged value writes nothing
        col.set_scheduling_preferences(prefs.clone())?;
        assert_eq!(col.scheduling_algorithm(), None);

        prefs.algorithm = Some(SchedulingAlgorithmProto::RwkvInstant as i32);
        col.set_scheduling_preferences(prefs.clone())?;
        assert_eq!(col.scheduling_algorithm(), Some(RwkvInstant));
        assert_eq!(preset_algorithms(&col), vec![RwkvInstant]);
        assert_eq!(
            col.get_scheduling_preferences()?.algorithm,
            Some(SchedulingAlgorithmProto::RwkvInstant as i32)
        );

        // a write without the field keeps the algorithm
        prefs.algorithm = None;
        col.set_scheduling_preferences(prefs)?;
        assert_eq!(col.scheduling_algorithm(), Some(RwkvInstant));
        Ok(())
    }

    // Pins spec/scheduling.md#sched.algorithm-change-prompt
    #[test]
    fn a_switch_to_fsrs7_recomputes_memory_states_and_the_reschedule_writes_no_review_log(
    ) -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        NoteAdder::basic(&mut col).add(&mut col);
        col.answer_good();
        let revlog_rows = col.storage.get_all_revlog_entries(TimestampSecs(0))?.len();
        col.change_scheduling_algorithm(RwkvCurve)?;
        // the reschedule belongs to FSRS-7 only
        assert!(col.reschedule_all_cards_with_fsrs7().is_err());
        let computed = col.get_first_card().memory_state.unwrap();

        // a stability RWKV-Curve wrote
        let mut card = col.get_first_card();
        card.memory_state.as_mut().unwrap().stability = 123.0;
        col.storage.update_card(&card)?;
        col.change_scheduling_algorithm(Fsrs7)?;
        let recomputed = col.get_first_card().memory_state.unwrap();
        assert_eq!(recomputed.stability, computed.stability);

        col.reschedule_all_cards_with_fsrs7()?;
        assert_eq!(
            col.storage.get_all_revlog_entries(TimestampSecs(0))?.len(),
            revlog_rows
        );
        Ok(())
    }
}
