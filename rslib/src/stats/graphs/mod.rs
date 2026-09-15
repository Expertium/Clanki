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
use anki_proto::stats::graphs_request::Graph;
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

/// The graphs a request asks for: every graph when it names none.
#[derive(Clone, Copy)]
struct WantedGraphs(u32);

impl WantedGraphs {
    fn new(graphs: &[i32]) -> Self {
        if graphs.is_empty() {
            return Self(u32::MAX);
        }
        Self(
            graphs
                .iter()
                .filter(|graph| (0..32).contains(*graph))
                .fold(0, |bits, graph| bits | 1 << graph),
        )
    }

    fn has(self, graph: Graph) -> bool {
        self.0 & 1 << graph as u32 != 0
    }

    fn any(self, graphs: &[Graph]) -> bool {
        graphs.iter().any(|graph| self.has(*graph))
    }
}

impl Collection {
    /// Every graph.
    #[cfg(test)]
    pub(crate) fn graph_data_for_search(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<anki_proto::stats::GraphsResponse> {
        self.graph_data_for_graphs(search, days, &[])
    }

    /// The graphs named in `graphs` (`GraphsRequest.Graph`); none = every
    /// graph.
    pub(crate) fn graph_data_for_graphs(
        &mut self,
        search: &str,
        days: u32,
        graphs: &[i32],
    ) -> Result<anki_proto::stats::GraphsResponse> {
        let wanted = WantedGraphs::new(graphs);
        if search.trim().is_empty() {
            // the whole collection: the cards and the review log are read
            // as they are, so they need not be searched into a table first
            return self.graph_data(search, true, 0, days, wanted);
        }
        let guard = self.search_cards_into_table_with_stats_search(
            search,
            SortMode::NoOrder,
            Some(search),
        )?;
        let searched_cards = guard.cards;
        guard
            .col
            .graph_data(search, false, searched_cards, days, wanted)
    }

    fn graph_data(
        &mut self,
        search: &str,
        all: bool,
        searched_cards: usize,
        days: u32,
        wanted: WantedGraphs,
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
        // only the graphs asked for read the review log or the cards
        let revlog = if !wanted.any(&[
            Graph::Reviews,
            Graph::TrueRetention,
            Graph::Today,
            Graph::Hours,
            Graph::Buttons,
        ]) {
            vec![]
        } else if all {
            self.storage.get_all_revlog_entries(revlog_start)?
        } else {
            let collection_cards: u32 =
                self.storage
                    .db
                    .query_row("select count() from cards", [], |row| row.get(0))?;
            self.storage
                .get_revlog_entries_for_searched_cards_after_stamp(
                    revlog_start,
                    searched_cards * 2 >= collection_cards as usize,
                )?
        };
        let load_cards = wanted.any(&[
            Graph::Added,
            Graph::FutureDue,
            Graph::Intervals,
            Graph::Stability,
            Graph::Eases,
            Graph::Difficulty,
            Graph::Retrievability,
        ]);
        let cards = if !load_cards {
            vec![]
        } else if all {
            self.storage.all_cards()?
        } else {
            self.storage.all_searched_cards()?
        };
        // without the cards (the Simple view), Card Counts counts in SQL
        let card_count_groups = if wanted.has(Graph::CardCounts) && !load_cards {
            Some(self.storage.card_count_groups(!all)?)
        } else {
            None
        };
        let algorithm = self.effective_scheduling_algorithm()?;
        let retrievability = wanted.has(Graph::Retrievability);
        let rwkv_retrievability_scores = match algorithm {
            _ if !retrievability => None,
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
        // only FSRS-7's retrievability reads the cards' FSRS presets
        if retrievability && algorithm == SchedulingAlgorithm::Fsrs7 {
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
        let (eases, difficulty) = if wanted.any(&[Graph::Eases, Graph::Difficulty]) {
            let (eases, difficulty) = ctx.eases();
            (Some(eases), Some(difficulty))
        } else {
            (None, None)
        };
        let resp = anki_proto::stats::GraphsResponse {
            added: wanted.has(Graph::Added).then(|| ctx.added_days()),
            reviews: wanted
                .has(Graph::Reviews)
                .then(|| ctx.review_counts_and_times()),
            true_retention: wanted
                .has(Graph::TrueRetention)
                .then(|| ctx.calculate_true_retention()),
            future_due: wanted.has(Graph::FutureDue).then(|| ctx.future_due()),
            intervals: wanted.has(Graph::Intervals).then(|| ctx.intervals()),
            // RWKV-Instant has no stability, RWKV no difficulty
            stability: (wanted.has(Graph::Stability)
                && algorithm != SchedulingAlgorithm::RwkvInstant)
                .then(|| ctx.stability()),
            eases: eases.filter(|_| wanted.has(Graph::Eases)),
            difficulty: difficulty.filter(|_| {
                wanted.has(Graph::Difficulty) && algorithm == SchedulingAlgorithm::Fsrs7
            }),
            today: wanted.has(Graph::Today).then(|| ctx.today()),
            hours: wanted.has(Graph::Hours).then(|| ctx.hours()),
            buttons: wanted.has(Graph::Buttons).then(|| ctx.buttons()),
            card_counts: match card_count_groups {
                Some(groups) => Some(card_counts::card_counts(groups.into_iter())),
                None => wanted.has(Graph::CardCounts).then(|| ctx.card_counts()),
            },
            rollover_hour: self.rollover_for_current_scheduler()? as u32,
            retrievability: retrievability.then(|| ctx.retrievability()),
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
    use anki_proto::stats::GraphsResponse;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::revlog::RevlogReviewKind;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;

    const ALL_GRAPHS: [Graph; 13] = [
        Graph::Added,
        Graph::Reviews,
        Graph::TrueRetention,
        Graph::FutureDue,
        Graph::Intervals,
        Graph::Stability,
        Graph::Eases,
        Graph::Difficulty,
        Graph::Today,
        Graph::Hours,
        Graph::Buttons,
        Graph::CardCounts,
        Graph::Retrievability,
    ];

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

    /// The graphs of `full` in `graphs`, and its always-sent fields.
    fn only(full: &GraphsResponse, graphs: &[Graph]) -> GraphsResponse {
        let has = |graph| graphs.contains(&graph);
        GraphsResponse {
            added: full.added.clone().filter(|_| has(Graph::Added)),
            reviews: full.reviews.clone().filter(|_| has(Graph::Reviews)),
            true_retention: full.true_retention.filter(|_| has(Graph::TrueRetention)),
            future_due: full.future_due.clone().filter(|_| has(Graph::FutureDue)),
            intervals: full.intervals.clone().filter(|_| has(Graph::Intervals)),
            stability: full.stability.clone().filter(|_| has(Graph::Stability)),
            eases: full.eases.clone().filter(|_| has(Graph::Eases)),
            difficulty: full.difficulty.clone().filter(|_| has(Graph::Difficulty)),
            today: full.today.filter(|_| has(Graph::Today)),
            hours: full.hours.clone().filter(|_| has(Graph::Hours)),
            buttons: full.buttons.clone().filter(|_| has(Graph::Buttons)),
            card_counts: full.card_counts.filter(|_| has(Graph::CardCounts)),
            retrievability: full
                .retrievability
                .clone()
                .filter(|_| has(Graph::Retrievability)),
            ..full.clone()
        }
    }

    fn ask(col: &mut Collection, search: &str, days: u32, graphs: &[Graph]) -> GraphsResponse {
        let graphs: Vec<i32> = graphs.iter().map(|graph| *graph as i32).collect();
        col.graph_data_for_graphs(search, days, &graphs).unwrap()
    }

    // A request naming graphs gets those graphs of the full response, the
    // same values, and no others.
    #[test]
    fn a_graph_list_gives_those_graphs_of_the_full_response() -> Result<()> {
        let mut col = collection_with_reviews()?;
        // FSRS-7: its retrievability depends on the second it is computed in
        let not_r: Vec<Graph> = ALL_GRAPHS
            .into_iter()
            .filter(|graph| *graph != Graph::Retrievability)
            .collect();
        for search in ["", "deck:Default", "-deck:Other is:review"] {
            for days in [365, 0] {
                let full = only(&ask(&mut col, search, days, &[]), &not_r);
                assert_eq!(ask(&mut col, search, days, &not_r), full);
                for graph in &not_r {
                    assert_eq!(
                        ask(&mut col, search, days, &[*graph]),
                        only(&full, &[*graph])
                    );
                }
                assert!(full.difficulty.is_some() && full.stability.is_some());
            }
        }
        // RWKV-Curve, scored: every value is exact, whenever it is computed
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        let scores = col
            .storage
            .get_all_cards()
            .iter()
            .enumerate()
            .map(|(i, card)| (card.id, [0.25, 0.5, 0.75][i % 3]))
            .collect();
        col.set_rwkv_stats_graph_scores("deck:Default".into(), scores)?;
        for search in ["", "deck:Default", "deck:Other", "-deck:Other is:review"] {
            for days in [365, 31, 0] {
                let full = ask(&mut col, search, days, &[]);
                assert_eq!(ask(&mut col, search, days, &ALL_GRAPHS), full);
                for graph in ALL_GRAPHS {
                    assert_eq!(ask(&mut col, search, days, &[graph]), only(&full, &[graph]));
                }
                let simple = [Graph::Reviews, Graph::CardCounts, Graph::TrueRetention];
                assert_eq!(ask(&mut col, search, days, &simple), only(&full, &simple));
                // the full response has every graph the algorithm shows
                assert!(full.added.is_some() && full.reviews.is_some());
                assert!(full.retrievability.is_some() && full.difficulty.is_none());
            }
        }
        Ok(())
    }

    // The whole collection, read as it is, gives what the searched cards give.
    #[test]
    fn the_empty_search_gives_what_a_search_of_every_card_gives() -> Result<()> {
        let mut col = collection_with_reviews()?;
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        let every_card = "deck:Default or deck:Other";
        assert_eq!(
            col.search_cards(every_card, SortMode::NoOrder)?.len(),
            col.search_cards("", SortMode::NoOrder)?.len()
        );
        // the same cards in the same order (the graphs add their values up
        // in that order)
        let guard = col.search_cards_into_table(every_card, SortMode::NoOrder)?;
        let searched: Vec<CardId> = guard
            .col
            .storage
            .all_searched_cards()?
            .iter()
            .map(|card| card.id)
            .collect();
        drop(guard);
        let every: Vec<CardId> = col
            .storage
            .all_cards()?
            .iter()
            .map(|card| card.id)
            .collect();
        assert_eq!(searched, every);
        for days in [365, 0] {
            for graphs in [
                ALL_GRAPHS.to_vec(),
                vec![Graph::Reviews, Graph::CardCounts, Graph::TrueRetention],
                vec![Graph::Added],
            ] {
                assert_eq!(
                    ask(&mut col, "", days, &graphs),
                    ask(&mut col, every_card, days, &graphs)
                );
            }
        }
        Ok(())
    }

    // Both ways of reading the searched cards' reviews give the same rows.
    #[test]
    fn searched_reviews_are_the_same_read_by_card_or_in_order() -> Result<()> {
        let mut col = collection_with_reviews()?;
        let start = TimestampSecs::now().adding_secs(-86_400 * 200);
        for search in [
            "",
            "deck:Default",
            "deck:Other",
            "is:suspended",
            "deck:none",
        ] {
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
