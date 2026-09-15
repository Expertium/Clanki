// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod added;
mod buttons;
mod card_counts;
mod eases;
mod future_due;
mod hours;
mod intervals;
mod retention;
mod retrievability;
mod reviews;
mod today;

use std::collections::HashMap;

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use fsrs::FSRS;

use crate::config::BoolKey;
use crate::config::Weekday;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::curve::Fsrs7Curve;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::search::SortMode;

struct GraphsContext {
    revlog: Vec<RevlogEntry>,
    cards: Vec<Card>,
    fsrs_by_preset: HashMap<FsrsPresetId, FSRS>,
    /// The same presets' curves in scalar form (bit-identical, faster).
    fsrs_curve_by_preset: HashMap<FsrsPresetId, Fsrs7Curve>,
    fsrs_preset_by_card: HashMap<CardId, FsrsPresetId>,
    /// The active algorithm's RWKV R per card (RWKV-Curve's curve R or
    /// RWKV-Instant's R); None under FSRS-7 and while RWKV has not scored
    /// the search yet (spec ui.stats-one-algorithm).
    rwkv_retrievability_scores: Option<HashMap<CardId, f32>>,
    algorithm: SchedulingAlgorithm,
    next_day_start: TimestampSecs,
    days_elapsed: u32,
    local_offset_secs: i64,
}

impl Collection {
    pub(crate) fn graph_data_for_search(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<anki_proto::stats::GraphsResponse> {
        let guard = self.search_cards_into_table_with_stats_search(
            search,
            SortMode::NoOrder,
            Some(search),
        )?;
        let all = search.trim().is_empty();
        let searched_cards = guard.cards;
        guard.col.graph_data(search, all, searched_cards, days)
    }

    fn graph_data(
        &mut self,
        search: &str,
        all: bool,
        searched_cards: usize,
        days: u32,
    ) -> Result<anki_proto::stats::GraphsResponse> {
        let timing = self.timing_today()?;
        let revlog_start = if days > 0 {
            timing
                .next_day_at
                .adding_secs(-(((days as i64) + 1) * 86_400))
        } else {
            TimestampSecs(0)
        };
        let offset = self.local_utc_offset_for_user()?;
        let local_offset_secs = offset.local_minus_utc() as i64;
        let revlog = if all {
            self.storage.get_all_revlog_entries(revlog_start)?
        } else {
            let collection_cards: u32 =
                self.storage
                    .db
                    .query_row("select count() from cards", [], |row| row.get(0))?;
            self.storage.get_revlog_entries_for_searched_cards_after_stamp(
                revlog_start,
                searched_cards * 2 >= collection_cards as usize,
            )?
        };
        let cards = self.storage.all_searched_cards()?;
        let algorithm = self.effective_scheduling_algorithm()?;
        let rwkv_retrievability_scores = match algorithm {
            SchedulingAlgorithm::Fsrs7 => None,
            SchedulingAlgorithm::RwkvInstant => {
                self.rwkv_stats_graph_scores_for_search(timing.days_elapsed, Some(search))
            }
            SchedulingAlgorithm::RwkvCurve => self
                .rwkv_stats_graph_score_entries_for_search(timing.days_elapsed, Some(search))
                .map(|entries| {
                    entries
                        .into_iter()
                        .filter_map(|(card_id, entry)| {
                            entry.curve_retrievability.map(|r| (card_id, r))
                        })
                        .collect()
                }),
        };
        let mut fsrs_by_preset = HashMap::new();
        let mut fsrs_curve_by_preset = HashMap::new();
        let mut fsrs_preset_by_card = HashMap::new();
        // only FSRS-7's retrievability reads the cards' FSRS presets; RWKV
        // shows none of FSRS-7's values (spec ui.stats-one-algorithm)
        if algorithm == SchedulingAlgorithm::Fsrs7 {
            let fsrs_cards: Vec<Card> = cards
                .iter()
                .filter(|card| card.memory_state.is_some())
                .cloned()
                .collect();
            let fsrs_preset_start = std::time::Instant::now();
            let fsrs_presets_by_card = self.fsrs_presets_for_cards(&fsrs_cards)?;
            tracing::debug!(
                searched_cards = cards.len(),
                fsrs_cards = fsrs_cards.len(),
                elapsed_ms = fsrs_preset_start.elapsed().as_secs_f64() * 1000.0,
                "resolved FSRS presets for stats graphs"
            );
            let fsrs_build_start = std::time::Instant::now();
            for (card_id, fsrs_preset) in fsrs_presets_by_card {
                let preset_id = fsrs_preset.id.clone();
                fsrs_preset_by_card.insert(card_id, preset_id.clone());
                if let std::collections::hash_map::Entry::Vacant(entry) =
                    fsrs_by_preset.entry(preset_id.clone())
                {
                    entry.insert(fsrs_preset.fsrs()?);
                    if let Some(curve) = Fsrs7Curve::new(&fsrs_preset.params) {
                        fsrs_curve_by_preset.insert(preset_id, curve);
                    }
                }
            }
            tracing::debug!(
                presets = fsrs_by_preset.len(),
                cards = fsrs_preset_by_card.len(),
                elapsed_ms = fsrs_build_start.elapsed().as_secs_f64() * 1000.0,
                "built FSRS instances for stats graphs"
            );
        }
        let ctx = GraphsContext {
            revlog,
            days_elapsed: timing.days_elapsed,
            cards,
            fsrs_by_preset,
            fsrs_curve_by_preset,
            fsrs_preset_by_card,
            rwkv_retrievability_scores,
            algorithm,
            next_day_start: timing.next_day_at,
            local_offset_secs,
        };
        let (eases, difficulty) = ctx.eases();
        let resp = anki_proto::stats::GraphsResponse {
            added: Some(ctx.added_days()),
            reviews: Some(ctx.review_counts_and_times()),
            true_retention: Some(ctx.calculate_true_retention()),
            future_due: Some(ctx.future_due()),
            intervals: Some(ctx.intervals()),
            // RWKV-Instant has no stability, RWKV no difficulty
            stability: (algorithm != SchedulingAlgorithm::RwkvInstant).then(|| ctx.stability()),
            eases: Some(eases),
            difficulty: (algorithm == SchedulingAlgorithm::Fsrs7).then_some(difficulty),
            today: Some(ctx.today()),
            hours: Some(ctx.hours()),
            buttons: Some(ctx.buttons()),
            card_counts: Some(ctx.card_counts()),
            rollover_hour: self.rollover_for_current_scheduler()? as u32,
            retrievability: Some(ctx.retrievability()),
            fsrs: self.get_config_bool(BoolKey::Fsrs),
            scheduling_algorithm: SchedulingAlgorithmProto::from(algorithm) as i32,
            advanced_ui: self.get_config_bool(BoolKey::AdvancedUi),
        };
        Ok(resp)
    }

    pub(crate) fn get_graph_preferences(&self) -> anki_proto::stats::GraphPreferences {
        anki_proto::stats::GraphPreferences {
            calendar_first_day_of_week: self.get_first_day_of_week() as i32,
            card_counts_separate_inactive: self
                .get_config_bool(BoolKey::CardCountsSeparateInactive),
            browser_links_supported: true,
            future_due_show_backlog: self.get_config_bool(BoolKey::FutureDueShowBacklog),
        }
    }

    pub(crate) fn set_graph_preferences(
        &mut self,
        prefs: anki_proto::stats::GraphPreferences,
    ) -> Result<()> {
        self.set_first_day_of_week(match prefs.calendar_first_day_of_week {
            1 => Weekday::Monday,
            5 => Weekday::Friday,
            6 => Weekday::Saturday,
            _ => Weekday::Sunday,
        })?;
        self.set_config_bool_inner(
            BoolKey::CardCountsSeparateInactive,
            prefs.card_counts_separate_inactive,
        )?;
        self.set_config_bool_inner(BoolKey::FutureDueShowBacklog, prefs.future_due_show_backlog)?;
        Ok(())
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::revlog::RevlogReviewKind;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;

    /// 40 cards in two decks, of every type and queue, with reviews of
    /// every kind over the last two years.
    fn collection_with_reviews() -> Result<Collection> {
        let mut col = Collection::new();
        let other = DeckAdder::new("Other").add(&mut col).id;
        let now = TimestampSecs::now();
        for i in 0..40i64 {
            let deck = if i % 3 == 0 { other } else { DeckId(1) };
            NoteAdder::basic(&mut col)
                .fields(&[&format!("front {i}"), "back"])
                .deck(deck)
                .add(&mut col);
        }
        let mut cards = col.storage.get_all_cards();
        cards.sort_by_key(|card| card.id);
        for (i, card) in cards.iter_mut().enumerate() {
            let i = i as i64;
            if i % 5 != 0 {
                card.ctype = [CardType::Learn, CardType::Review, CardType::Relearn][i as usize % 3];
                card.queue = match i % 7 {
                    0 => CardQueue::Suspended,
                    1 => CardQueue::UserBuried,
                    _ => CardQueue::Review,
                };
                card.interval = (i * 13 % 90) as u32;
                card.due = (i * 7 % 50) as i32;
                card.ease_factor = 1300 + (i * 97 % 1500) as u16;
                if i % 2 == 0 {
                    card.memory_state = Some(FsrsMemoryState {
                        stability: 1.0 + (i * 3) as f32,
                        stability_internal: 1.0 + (i * 3) as f32,
                        stability_fast: None,
                        difficulty: 1.0 + (i % 9) as f32,
                    });
                    card.last_review_time = Some(now.adding_secs(-86_400 * (i % 30)));
                }
            }
            col.storage.update_card(card)?;
            for review in 0..(i % 6) {
                let kind = [
                    RevlogReviewKind::Learning,
                    RevlogReviewKind::Review,
                    RevlogReviewKind::Relearning,
                    RevlogReviewKind::Filtered,
                    RevlogReviewKind::Manual,
                    RevlogReviewKind::Rescheduled,
                ][((i + review) % 6) as usize];
                col.storage.add_revlog_entry(
                    &RevlogEntry {
                        id: RevlogId(
                            now.adding_secs(-(i * 17 + review * 101) * 3_600).0 * 1000
                                + i * 10
                                + review,
                        ),
                        cid: card.id,
                        button_chosen: ((i + review) % 5) as u8,
                        interval: (i * review) as i32,
                        last_interval: [-600, 0, 1, 20, 21, 45][((i * 3 + review) % 6) as usize],
                        ease_factor: 2500,
                        taken_millis: (i * 1000 + review) as u32,
                        review_kind: kind,
                        ..Default::default()
                    },
                    false,
                )?;
            }
        }
        Ok(col)
    }

    // Both ways of reading the searched cards' reviews give the same rows.
    #[test]
    fn searched_reviews_are_the_same_read_by_card_or_in_order() -> Result<()> {
        let mut col = collection_with_reviews()?;
        let start = TimestampSecs::now().adding_secs(-86_400 * 200);
        for search in ["", "deck:Default", "deck:Other", "is:suspended", "deck:none"] {
            for after in [TimestampSecs(0), start] {
                let guard = col.search_cards_into_table(search, SortMode::NoOrder)?;
                let mut by_card = guard
                    .col
                    .storage
                    .get_revlog_entries_for_searched_cards_after_stamp(after, false)?;
                let mut in_order = guard
                    .col
                    .storage
                    .get_revlog_entries_for_searched_cards_after_stamp(after, true)?;
                by_card.sort_by_key(|entry| entry.id);
                in_order.sort_by_key(|entry| entry.id);
                assert_eq!(by_card, in_order);
                assert!(search == "deck:none" || !by_card.is_empty());
            }
        }
        Ok(())
    }

    // Pins spec/ui.md#ui.mode-switch: the Stats page learns the UI mode
    // with its data.
    #[test]
    fn graphs_report_the_ui_mode() -> Result<()> {
        let mut col = Collection::new();
        assert!(!col.graph_data_for_search("", 365)?.advanced_ui);
        col.set_config_bool(BoolKey::AdvancedUi, true, false)?;
        assert!(col.graph_data_for_search("", 365)?.advanced_ui);
        Ok(())
    }
}
