// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod burying;
mod gathering;
pub(crate) mod intersperser;
pub(crate) mod sized_chain;
mod sorting;

use std::collections::HashMap;
use std::collections::VecDeque;
use std::sync::Arc;

use intersperser::Intersperser;
use sized_chain::SizedChain;

use super::BuryMode;
use super::CardQueues;
use super::Counts;
use super::DeferredRwkvReview;
use super::LearningQueueEntry;
use super::MainQueueEntry;
use super::MainQueueEntryKind;
use crate::card::CardQueue;
use crate::collection::RwkvReviewQueueScoreEntry;
use crate::deckconfig::NewCardGatherPriority;
use crate::deckconfig::NewCardSortOrder;
use crate::deckconfig::ReviewCardOrder;
use crate::deckconfig::ReviewMix;
use crate::decks::limits::LimitTreeMap;
use crate::prelude::*;
use crate::scheduler::states::load_balancer::LoadBalancer;
use crate::scheduler::timing::SchedTimingToday;

/// Temporary holder for review cards that will be built into a queue.
#[derive(Debug, Clone, Copy)]
pub(crate) struct DueCard {
    pub id: CardId,
    pub note_id: NoteId,
    pub mtime: TimestampSecs,
    pub due: i32,
    pub current_deck_id: DeckId,
    pub original_deck_id: DeckId,
    pub kind: DueCardKind,
    pub reps: u32,
}

impl DueCard {
    /// The entry `for_each_due_card_in_active_decks` makes of the card's row.
    pub(crate) fn from_card(card: &Card, kind: DueCardKind) -> Self {
        DueCard {
            id: card.id,
            note_id: card.note_id,
            mtime: card.mtime,
            due: card.due,
            current_deck_id: card.deck_id,
            original_deck_id: card.original_deck_id,
            kind,
            reps: card.reps,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum DueCardKind {
    Review,
    Learning,
}

/// Temporary holder for new cards that will be built into a queue.
#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct NewCard {
    pub id: CardId,
    pub note_id: NoteId,
    pub mtime: TimestampSecs,
    pub current_deck_id: DeckId,
    pub original_deck_id: DeckId,
    pub template_index: u32,
    pub hash: u64,
}

impl From<DueCard> for MainQueueEntry {
    fn from(c: DueCard) -> Self {
        MainQueueEntry {
            id: c.id,
            mtime: c.mtime,
            kind: match c.kind {
                DueCardKind::Review => MainQueueEntryKind::Review,
                DueCardKind::Learning => MainQueueEntryKind::InterdayLearning,
            },
        }
    }
}

impl From<NewCard> for MainQueueEntry {
    fn from(c: NewCard) -> Self {
        MainQueueEntry {
            id: c.id,
            mtime: c.mtime,
            kind: MainQueueEntryKind::New,
        }
    }
}

impl From<DueCard> for LearningQueueEntry {
    fn from(c: DueCard) -> Self {
        LearningQueueEntry {
            due: TimestampSecs(c.due as i64),
            id: c.id,
            mtime: c.mtime,
            reps: c.reps,
        }
    }
}

#[derive(Default, Clone, Debug)]
pub(super) struct QueueSortOptions {
    pub(super) new_order: NewCardSortOrder,
    pub(super) new_gather_priority: NewCardGatherPriority,
    pub(super) review_order: ReviewCardOrder,
    pub(super) day_learn_mix: ReviewMix,
    pub(super) new_review_mix: ReviewMix,
    pub(super) rwkv_review_enabled: bool,
    pub(super) rwkv_review_instant_order_enabled: bool,
    pub(super) rwkv_review_allow_same_day_review: bool,
    pub(super) rwkv_review_min_intervening_reviews: u32,
    pub(super) rwkv_review_min_elapsed_secs: u32,
}

#[derive(Debug)]
pub(super) struct QueueBuilder {
    pub(super) new: Vec<NewCard>,
    pub(super) review: Vec<DueCard>,
    pub(super) learning: Vec<DueCard>,
    pub(super) day_learning: Vec<DueCard>,
    pub(super) r_sorted_non_new: Vec<DueCard>,
    pinned_card_id: Option<CardId>,
    deferred_rwkv_reviews: HashMap<CardId, DeferredRwkvReview>,
    limits: LimitTreeMap,
    load_balancer: Option<LoadBalancer>,
    context: Context,
}

/// Data container and helper for building queues.
#[derive(Debug, Clone)]
struct Context {
    timing: SchedTimingToday,
    config_map: HashMap<DeckConfigId, DeckConfig>,
    root_deck: Deck,
    sort_options: QueueSortOptions,
    seen_note_ids: HashMap<NoteId, BuryMode>,
    deck_map: HashMap<DeckId, Deck>,
    fsrs: bool,
    fsrs_short_term_with_steps: bool,
    rwkv_review_queue_scores: Option<Arc<HashMap<CardId, RwkvReviewQueueScoreEntry>>>,
}

impl QueueBuilder {
    pub(super) fn new(col: &mut Collection, deck_id: DeckId) -> Result<Self> {
        let timing = col.timing_for_timestamp(TimestampSecs::now())?;
        let new_cards_ignore_review_limit = col.get_config_bool(BoolKey::NewCardsIgnoreReviewLimit);
        let apply_all_parent_limits = col.get_config_bool(BoolKey::ApplyAllParentLimits);
        let config_map = col.storage.get_deck_config_map()?;
        let root_deck = col.storage.get_deck(deck_id)?.or_not_found(deck_id)?;
        let mut decks = col.storage.child_decks(&root_deck)?;
        decks.insert(0, root_deck.clone());
        if apply_all_parent_limits {
            for parent in col.storage.parent_decks(&root_deck)? {
                decks.insert(0, parent);
            }
        }
        let mut limits = LimitTreeMap::build(
            &decks,
            &config_map,
            timing.days_elapsed,
            new_cards_ignore_review_limit,
        );
        // the reservation only lowers RWKV review minimums: without any, skip
        // its scan of every card
        if limits.any_rwkv_review_minimum_remaining() {
            for (original_deck_id, count) in
                col.storage.filtered_review_counts_by_original_deck()?
            {
                limits.reserve_rwkv_reviews_if_present(original_deck_id, count);
            }
        }
        let sort_options = sort_options(&root_deck, &config_map);
        let rwkv_review_queue_scores = if sort_options.uses_rwkv_retrievability_scores() {
            if let Some(scores) = col.rwkv_review_queue_scores(root_deck.id, timing.days_elapsed) {
                Some(scores)
            } else if let Some((score_deck_id, scores)) =
                col.rwkv_review_queue_scores_for_day(timing.days_elapsed)
            {
                col.storage
                    .get_deck(score_deck_id)?
                    .filter(|score_root| deck_is_within_scope(&root_deck, score_root))
                    .map(|_| scores)
            } else {
                None
            }
        } else {
            None
        };
        let deck_map = col.storage.get_decks_map()?;

        let load_balancer = col
            .get_config_bool(BoolKey::LoadBalancerEnabled)
            .then(|| {
                let did_to_dcid = deck_map
                    .values()
                    .filter_map(|deck| Some((deck.id, deck.config_id()?)))
                    .collect::<HashMap<_, _>>();
                LoadBalancer::new(
                    timing.days_elapsed,
                    did_to_dcid,
                    col.review_fuzz_config(),
                    col.timing_today()?.next_day_at,
                    &col.storage,
                )
            })
            .transpose()?;

        Ok(QueueBuilder {
            new: Vec::new(),
            review: Vec::new(),
            learning: Vec::new(),
            day_learning: Vec::new(),
            r_sorted_non_new: Vec::new(),
            pinned_card_id: None,
            deferred_rwkv_reviews: HashMap::new(),
            limits,
            load_balancer,
            context: Context {
                timing,
                config_map,
                root_deck,
                sort_options,
                seen_note_ids: HashMap::new(),
                deck_map,
                fsrs: col.get_config_bool(BoolKey::Fsrs),
                fsrs_short_term_with_steps: col
                    .get_config_bool(BoolKey::FsrsShortTermWithStepsEnabled),
                rwkv_review_queue_scores,
            },
        })
    }

    fn pin_current_card(&mut self, card: &Card) -> Result<()> {
        require!(
            self.pinned_card_id.is_none(),
            "a queue can only preserve one current card"
        );

        let current_deck_id = card.deck_id;
        let original_deck_id = card.original_deck_id;
        match card.queue {
            CardQueue::New => {
                let card = NewCard {
                    id: card.id,
                    note_id: card.note_id,
                    mtime: card.mtime,
                    current_deck_id,
                    original_deck_id,
                    template_index: card.template_idx as u32,
                    hash: 0,
                };
                require!(self.add_new_card(card), "current new card was buried");
                self.limits.reserve_new_card(current_deck_id)?;
            }
            CardQueue::Learn | CardQueue::PreviewRepeat => {
                let card = DueCard {
                    id: card.id,
                    note_id: card.note_id,
                    mtime: card.mtime,
                    due: card.due,
                    current_deck_id,
                    original_deck_id,
                    kind: DueCardKind::Learning,
                    reps: card.reps,
                };
                self.get_and_update_bury_mode_for_note(card.into());
                self.learning.push(card);
            }
            CardQueue::Review | CardQueue::DayLearn => {
                let card = DueCard {
                    id: card.id,
                    note_id: card.note_id,
                    mtime: card.mtime,
                    due: card.due,
                    current_deck_id,
                    original_deck_id,
                    kind: if card.queue == CardQueue::Review {
                        DueCardKind::Review
                    } else {
                        DueCardKind::Learning
                    },
                    reps: card.reps,
                };
                if self.context.non_news_sorted_by_retrievability() {
                    require!(
                        self.add_due_card_for_retrievability_sort(card),
                        "current review card was buried"
                    );
                    self.r_sorted_non_new.push(card);
                } else {
                    require!(self.add_due_card(card), "current review card was buried");
                }
                self.limits
                    .reserve_review(current_deck_id, original_deck_id)?;
            }
            CardQueue::Suspended | CardQueue::SchedBuried | CardQueue::UserBuried => {
                invalid_input!("current card is not eligible for the review queue")
            }
        }

        self.pinned_card_id = Some(card.id);
        Ok(())
    }

    fn card_is_pinned(&self, card_id: CardId) -> bool {
        self.pinned_card_id == Some(card_id)
    }

    pub(super) fn build(mut self, learn_ahead_secs: i64) -> CardQueues {
        self.sort_new();

        // intraday learning and total learn count
        let intraday_learning = sort_learning(self.learning);
        let now = TimestampSecs::now();
        let cutoff = now.adding_secs(learn_ahead_secs);
        // the retrievability orders rank interday learning and review cards
        // together; intraday learning cards keep their due times either way
        let shared_r_sort = self.context.non_news_sorted_by_retrievability();
        let (day_learning_count, review_count) = if shared_r_sort {
            let day_learning_count = self
                .r_sorted_non_new
                .iter()
                .filter(|card| matches!(card.kind, DueCardKind::Learning))
                .count();
            (
                day_learning_count,
                self.r_sorted_non_new.len() - day_learning_count,
            )
        } else {
            (self.day_learning.len(), self.review.len())
        };
        let learn_count =
            intraday_learning.iter().filter(|e| e.due <= cutoff).count() + day_learning_count;
        let new_count = self.new.len();

        // merge due non-new and new cards into main
        let with_interday_learn = if shared_r_sort {
            Box::new(self.r_sorted_non_new.into_iter().map(Into::into))
                as Box<dyn ExactSizeIterator<Item = MainQueueEntry>>
        } else {
            merge_day_learning(
                self.review,
                self.day_learning,
                self.context.sort_options.day_learn_mix,
            )
        };
        let main_iter = merge_new(
            with_interday_learn,
            self.new,
            self.context.sort_options.new_review_mix,
        );

        CardQueues {
            counts: Counts {
                new: new_count,
                review: review_count,
                learning: learn_count,
            },
            main: main_iter.collect(),
            intraday_learning,
            learn_ahead_secs,
            current_day: self.context.timing.days_elapsed,
            build_time: TimestampMillis::now(),
            load_balancer: self.load_balancer,
            fsrs_enabled: self.context.fsrs,
            fsrs_short_term_with_steps: self.context.fsrs_short_term_with_steps,
            current_learning_cutoff: now,
            shown_top_card: None,
            deferred_rwkv_reviews: self.deferred_rwkv_reviews,
            rwkv_scores_pending: self.context.rwkv_scores_pending(),
        }
    }
}

fn deck_is_within_scope(deck: &Deck, scope_root: &Deck) -> bool {
    let mut deck_components = deck.name.components();
    scope_root
        .name
        .components()
        .all(|component| deck_components.next() == Some(component))
}

impl Context {
    fn non_news_sorted_by_retrievability(&self) -> bool {
        self.fsrs
            && !self.sort_options.uses_rwkv_review_order()
            && !self.sort_options.rwkv_review_enabled
            && matches!(
                self.sort_options.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
                    | ReviewCardOrder::RelativeOverdueness
            )
    }

    fn uses_rwkv_review_order(&self) -> bool {
        self.rwkv_review_queue_scores.is_some() && self.sort_options.uses_rwkv_review_order()
    }

    /// RWKV-Instant takes review cards only from its scores. Without scores
    /// for the studied deck it gathers none, and the client waits (spec
    /// sched.rwkv-instant-waits).
    fn rwkv_scores_pending(&self) -> bool {
        self.sort_options.uses_rwkv_review_order() && self.rwkv_review_queue_scores.is_none()
    }
}

impl QueueSortOptions {
    fn uses_rwkv_review_order(&self) -> bool {
        self.rwkv_review_instant_order_enabled
    }

    /// Only RWKV-Instant reads its scores; the retrievability new-card
    /// orders under FSRS-7 or RWKV-Curve would otherwise rank by
    /// RWKV-Instant's values (spec
    /// deck-options.new-retrievability-order-instant-only).
    fn uses_rwkv_retrievability_scores(&self) -> bool {
        self.uses_rwkv_review_order()
    }

    /// Retrievability and relative-overdueness orders in an RWKV preset rank
    /// by RWKV's own measure (spec sched.rwkv-review-order), never FSRS's.
    fn review_order_from_rwkv_keys(&self) -> bool {
        (self.rwkv_review_enabled || self.rwkv_review_instant_order_enabled)
            && matches!(
                self.review_order,
                ReviewCardOrder::RetrievabilityAscending
                    | ReviewCardOrder::RetrievabilityDescending
                    | ReviewCardOrder::RelativeOverdueness
            )
    }
}

fn sort_options(deck: &Deck, config_map: &HashMap<DeckConfigId, DeckConfig>) -> QueueSortOptions {
    deck.config_id()
        .and_then(|config_id| config_map.get(&config_id))
        .map(|config| QueueSortOptions {
            new_order: config.inner.new_card_sort_order(),
            new_gather_priority: config.inner.new_card_gather_priority(),
            review_order: config.inner.review_order(),
            day_learn_mix: config.inner.interday_learning_mix(),
            new_review_mix: config.inner.new_mix(),
            rwkv_review_enabled: config.inner.rwkv_review_enabled,
            rwkv_review_instant_order_enabled: config.inner.rwkv_review_instant_order_enabled,
            rwkv_review_allow_same_day_review: config.inner.rwkv_review_allow_same_day_review,
            rwkv_review_min_intervening_reviews: config.inner.rwkv_review_min_intervening_reviews,
            rwkv_review_min_elapsed_secs: config.inner.rwkv_review_min_elapsed_secs,
        })
        .unwrap_or_else(|| {
            // filtered decks do not space siblings
            QueueSortOptions {
                new_order: NewCardSortOrder::NoSort,
                ..Default::default()
            }
        })
}

fn merge_day_learning(
    reviews: Vec<DueCard>,
    day_learning: Vec<DueCard>,
    mode: ReviewMix,
) -> Box<dyn ExactSizeIterator<Item = MainQueueEntry>> {
    let day_learning_iter = day_learning.into_iter().map(Into::into);
    let reviews_iter = reviews.into_iter().map(Into::into);

    match mode {
        ReviewMix::AfterReviews => Box::new(SizedChain::new(reviews_iter, day_learning_iter)),
        ReviewMix::BeforeReviews => Box::new(SizedChain::new(day_learning_iter, reviews_iter)),
        ReviewMix::MixWithReviews => Box::new(Intersperser::new(reviews_iter, day_learning_iter)),
    }
}

fn merge_new(
    review_iter: impl ExactSizeIterator<Item = MainQueueEntry> + 'static,
    new: Vec<NewCard>,
    mode: ReviewMix,
) -> Box<dyn ExactSizeIterator<Item = MainQueueEntry>> {
    let new_iter = new.into_iter().map(Into::into);

    match mode {
        ReviewMix::BeforeReviews => Box::new(SizedChain::new(new_iter, review_iter)),
        ReviewMix::AfterReviews => Box::new(SizedChain::new(review_iter, new_iter)),
        ReviewMix::MixWithReviews => Box::new(Intersperser::new(review_iter, new_iter)),
    }
}

fn sort_learning(learning: Vec<DueCard>) -> VecDeque<LearningQueueEntry> {
    let mut entries: Vec<LearningQueueEntry> =
        learning.into_iter().map(LearningQueueEntry::from).collect();
    entries.sort_by(|a, b| a.cmp_by_reps_then_due(b));
    entries.into_iter().collect()
}

impl Collection {
    pub(crate) fn build_queues(&mut self, deck_id: DeckId) -> Result<CardQueues> {
        self.build_queues_with_current_card(deck_id, None)
    }

    pub(crate) fn build_queues_with_current_card(
        &mut self,
        deck_id: DeckId,
        current_card: Option<&Card>,
    ) -> Result<CardQueues> {
        let mut queues = QueueBuilder::new(self, deck_id)?;
        self.storage
            .update_active_decks(&queues.context.root_deck)?;
        let current_card = current_card.filter(|card| {
            queues
                .context
                .deck_map
                .get(&card.deck_id)
                .is_some_and(|deck| deck_is_within_scope(deck, &queues.context.root_deck))
        });

        if let Some(card) = current_card {
            queues.pin_current_card(card)?;
        }
        queues.gather_cards(self)?;

        let mut queues = queues.build(self.learn_ahead_secs() as i64);
        if let Some(card) = current_card {
            queues.preserve_current_card(card.id)?;
        }

        Ok(queues)
    }
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;
    use std::collections::HashSet;
    use std::hash::Hasher;

    use anki_proto::deck_config::deck_config::config::NewCardGatherPriority;
    use anki_proto::deck_config::deck_config::config::NewCardSortOrder;
    use fnv::FnvHasher;
    use fsrs::DEFAULT_PARAMETERS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::FsrsVersion;
    use crate::search::SortMode;

    impl Collection {
        fn set_deck_gather_order(&mut self, deck: &mut Deck, order: NewCardGatherPriority) {
            let mut conf = DeckConfig::default();
            // these tests count sibling cards in the queue, and were written
            // before siblings were buried by default
            // (spec deck-options.new-preset-defaults)
            conf.inner.bury_new = false;
            conf.inner.bury_reviews = false;
            conf.inner.bury_interday_learning = false;
            conf.inner.new_card_gather_priority = order as i32;
            conf.inner.new_card_sort_order = NewCardSortOrder::NoSort as i32;
            // the retrievability gather orders are RWKV-Instant's (spec
            // deck-options.new-retrievability-order-instant-only)
            if matches!(
                order,
                NewCardGatherPriority::AscendingRetrievability
                    | NewCardGatherPriority::DescendingRetrievability
            ) {
                conf.inner.rwkv_review_enabled = false;
                conf.inner.rwkv_review_instant_order_enabled = true;
            }
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_new_limit(&mut self, deck: &mut Deck, new_limit: u32) {
            let mut conf = DeckConfig::default();
            conf.inner.bury_new = false;
            conf.inner.bury_reviews = false;
            conf.inner.bury_interday_learning = false;
            conf.inner.new_per_day = new_limit;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_review_limit(&mut self, deck: DeckId, limit: u32) {
            let dcid = self.get_deck(deck).unwrap().unwrap().config_id().unwrap();
            let mut conf = self.get_deck_config(dcid, false).unwrap().unwrap();
            conf.inner.reviews_per_day = limit;
            self.add_or_update_deck_config(&mut conf).unwrap();
        }

        fn set_deck_fsrs7_defaults(&mut self, deck: DeckId) {
            let config_id = self.get_deck(deck).unwrap().unwrap().config_id().unwrap();
            let mut config = self.get_deck_config(config_id, false).unwrap().unwrap();
            config.inner.fsrs_version = FsrsVersion::Seven as i32;
            config.inner.fsrs_params_7 = DEFAULT_PARAMETERS.to_vec();
            self.add_or_update_deck_config(&mut config).unwrap();
        }

        fn queue_as_deck_and_template(&mut self, deck_id: DeckId) -> Vec<(DeckId, u16)> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| {
                    let card = self.storage.get_card(entry.card_id()).unwrap().unwrap();
                    (card.deck_id, card.template_idx)
                })
                .collect()
        }

        fn set_deck_review_order(&mut self, deck: &mut Deck, order: ReviewCardOrder) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            // These tests describe FSRS ordering; a new preset runs RWKV-Curve
            // (spec deck-options.new-preset-defaults), which gathers by day.
            conf.inner.rwkv_review_enabled = false;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_instant_order(&mut self, deck: &mut Deck, order: ReviewCardOrder) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = false;
            conf.inner.rwkv_review_instant_order_enabled = true;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_review_order_with_desired_retention(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
        ) {
            self.set_deck_rwkv_review_order_with_options(deck, order, desired_retention, false);
        }

        fn set_deck_rwkv_review_order_with_options(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
            allow_same_day_review: bool,
        ) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = false;
            conf.inner.rwkv_review_instant_order_enabled = true;
            conf.inner.desired_retention = desired_retention;
            conf.inner.rwkv_review_allow_same_day_review = allow_same_day_review;
            conf.inner.rwkv_review_min_intervening_reviews = 0;
            conf.inner.rwkv_review_min_elapsed_secs = 0;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_review_order_with_repeat_guards(
            &mut self,
            deck: &mut Deck,
            order: ReviewCardOrder,
            desired_retention: f32,
            min_intervening_reviews: u32,
            min_elapsed_secs: u32,
        ) {
            let mut conf = DeckConfig::default();
            conf.inner.review_order = order as i32;
            conf.inner.rwkv_review_enabled = false;
            conf.inner.rwkv_review_instant_order_enabled = true;
            conf.inner.desired_retention = desired_retention;
            conf.inner.rwkv_review_allow_same_day_review = true;
            conf.inner.rwkv_review_min_intervening_reviews = min_intervening_reviews;
            conf.inner.rwkv_review_min_elapsed_secs = min_elapsed_secs;
            self.add_or_update_deck_config(&mut conf).unwrap();
            deck.normal_mut().unwrap().config_id = conf.id.0;
            self.add_or_update_deck(deck).unwrap();
        }

        fn set_deck_rwkv_minimum_reviews(&mut self, deck: DeckId, minimum: u32) {
            let config_id = self.get_deck(deck).unwrap().unwrap().config_id().unwrap();
            let mut config = self.get_deck_config(config_id, false).unwrap().unwrap();
            config.inner.rwkv_review_minimum_reviews_per_day = minimum;
            self.add_or_update_deck_config(&mut config).unwrap();
        }

        fn queue_as_due_and_ivl(&mut self, deck_id: DeckId) -> Vec<(i32, u32)> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| {
                    let card = self.storage.get_card(entry.card_id()).unwrap().unwrap();
                    (card.due, card.interval)
                })
                .collect()
        }

        fn queue_as_ids(&mut self, deck_id: DeckId) -> Vec<CardId> {
            self.build_queues(deck_id)
                .unwrap()
                .iter()
                .map(|entry| entry.card_id())
                .collect()
        }

        fn queued_card_ids(&mut self, fetch_limit: usize) -> Result<Vec<CardId>> {
            Ok(self
                .get_queued_cards(fetch_limit, false, true)?
                .cards
                .into_iter()
                .map(|queued| queued.card.id)
                .collect())
        }
    }

    // Pins spec/scheduling.md#sched.study-queue-kept-after-answer: after an
    // answer the reviewer empties an RWKV score map that is already empty;
    // the queue is kept (a card that reached the deck without an operation
    // stays out of it), while installing scores still builds it again.
    #[test]
    fn emptying_empty_rwkv_scores_keeps_the_study_queue() -> Result<()> {
        let mut col = Collection::new();
        let deck_id = DeckId(1);
        let elsewhere = col.get_or_create_normal_deck("Elsewhere")?.id;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut card_ids = Vec::new();
        for (index, deck) in [deck_id, deck_id, elsewhere].into_iter().enumerate() {
            let mut note = nt.new_note();
            note.set_field(0, format!("front {index}"))?;
            col.add_note(&mut note, deck)?;
            card_ids.push(col.storage.get_card_by_ordinal(note.id, 0)?.unwrap().id);
        }
        col.set_current_deck(deck_id)?;
        assert_eq!(col.get_queued_cards(1, false, true)?.new_count, 2);

        col.answer_good();
        // the card moves into the deck behind the queue's back
        let mut late_card = col.storage.get_card(card_ids[2])?.unwrap();
        late_card.deck_id = deck_id;
        col.storage.update_card(&late_card)?;
        col.set_rwkv_review_queue_score_entries(deck_id, HashMap::new())?;
        assert_eq!(col.get_queued_cards(1, false, true)?.new_count, 1);

        col.set_rwkv_review_queue_scores(deck_id, HashMap::from([(card_ids[1], 0.5)]))?;
        assert_eq!(col.get_queued_cards(1, false, true)?.new_count, 2);
        Ok(())
    }

    #[test]
    fn queued_cards_can_skip_scheduling_states() {
        let mut col = Collection::new();
        CardAdder::new().add(&mut col);

        let queued = col.get_queued_cards(1, false, true).unwrap();
        assert_eq!(queued.cards.len(), 1);
        assert!(queued.cards[0].states.is_none());

        let queued = col.get_queued_cards(1, false, false).unwrap();
        assert_eq!(queued.cards.len(), 1);
        assert!(queued.cards[0].states.is_some());
    }

    #[test]
    fn preserved_current_card_reuses_rebuilt_rwkv_queue() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let current = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let next = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let last = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(current, 0.10), (next, 0.20), (last, 0.30)]),
        )?;
        assert_eq!(col.queued_card_ids(1)?, vec![current]);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(current, 0.80), (next, 0.10), (last, 0.20)]),
        )?;
        let counts = col.rebuild_queued_cards_preserving_current_card(current)?;
        assert!(counts.cards.is_empty());
        assert_eq!(
            (counts.new_count, counts.learning_count, counts.review_count),
            (0, 0, 3)
        );

        let cached = col.state.card_queues.as_ref().unwrap();
        let build_time = cached.build_time;
        assert_eq!(cached.shown_top_card, Some(current));
        assert_eq!(cached.main.front().unwrap().id, current);

        let restored = col.get_queued_cards(1, false, true)?;
        assert_eq!(restored.cards[0].card.id, current);
        assert_eq!(restored.review_count, 3);

        col.answer_good();
        let next_card = col.get_queued_cards(1, false, true)?;
        assert_eq!(next_card.cards[0].card.id, next);
        assert_eq!(
            col.state.card_queues.as_ref().unwrap().build_time,
            build_time
        );
        Ok(())
    }

    #[test]
    fn preserved_current_card_outside_selected_deck_is_ignored() -> Result<()> {
        let mut col = Collection::new();
        let selected_deck = col.get_or_create_normal_deck("Selected")?;
        let other_deck = col.get_or_create_normal_deck("Other")?;
        col.set_current_deck(selected_deck.id)?;
        let selected_card = CardAdder::new().deck(selected_deck.id).add(&mut col)[0].id;
        let other_card = CardAdder::new().deck(other_deck.id).add(&mut col)[0].id;

        let counts = col.rebuild_queued_cards_preserving_current_card(other_card)?;

        assert_eq!(counts.new_count, 1);
        assert_eq!(col.queued_card_ids(10)?, vec![selected_card]);
        Ok(())
    }

    #[test]
    fn queue_tolerates_missing_intermediate_parent_deck() -> Result<()> {
        let mut col = Collection::new();
        let root = col.get_or_create_normal_deck("Root")?;
        let missing_parent = col.get_or_create_normal_deck("Root::Missing")?;
        let leaf = col.get_or_create_normal_deck("Root::Missing::Leaf")?;
        col.storage.remove_deck(missing_parent.id)?;
        col.set_current_deck(root.id)?;
        let card_id = CardAdder::new().deck(leaf.id).add(&mut col)[0].id;

        assert_eq!(col.queued_card_ids(10)?, vec![card_id]);
        Ok(())
    }

    #[test]
    fn preserved_current_card_applies_sibling_burying_first() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        let config_id = deck.config_id().unwrap();
        let mut config = col.get_deck_config(config_id, false)?.unwrap();
        config.inner.bury_reviews = true;
        col.add_or_update_deck_config(&mut config)?;

        let siblings = CardAdder::new()
            .siblings(2)
            .deck(deck.id)
            .due_dates(["0", "0"])
            .add(&mut col);
        let current = siblings[0].id;
        let sibling = siblings[1].id;
        let unrelated = CardAdder::new()
            .deck(deck.id)
            .due_dates(["0"])
            .add(&mut col)[0]
            .id;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(current, 0.10), (sibling, 0.20), (unrelated, 0.30)]),
        )?;
        assert_eq!(col.queued_card_ids(1)?, vec![current]);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(current, 0.80), (sibling, 0.10), (unrelated, 0.20)]),
        )?;
        let counts = col.rebuild_queued_cards_preserving_current_card(current)?;
        assert_eq!(counts.review_count, 2);
        assert_eq!(col.queued_card_ids(10)?, vec![current, unrelated]);
        Ok(())
    }

    #[test]
    fn preserved_current_new_card_reserves_review_capacity() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        let config_id = deck.config_id().unwrap();
        let mut config = col.get_deck_config(config_id, false)?.unwrap();
        config.inner.new_mix = ReviewMix::BeforeReviews as i32;
        config.inner.new_per_day = 1;
        config.inner.reviews_per_day = 2;
        col.add_or_update_deck_config(&mut config)?;

        let current = CardAdder::new().deck(deck.id).add(&mut col)[0].id;
        let timing = col.timing_today()?;
        let first_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let second_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(first_review, 0.95), (second_review, 0.95)]),
        )?;
        assert_eq!(col.queued_card_ids(1)?, vec![current]);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(first_review, 0.10), (second_review, 0.20)]),
        )?;
        let counts = col.rebuild_queued_cards_preserving_current_card(current)?;
        assert_eq!(
            (counts.new_count, counts.learning_count, counts.review_count),
            (1, 0, 1)
        );

        col.answer_good();
        let next_card = col.get_queued_cards(1, false, true)?;
        assert_eq!(next_card.cards[0].card.id, first_review);
        Ok(())
    }

    #[test]
    fn should_build_empty_queue_if_limit_is_reached() {
        let mut col = Collection::new();
        CardAdder::new().due_dates(["0"]).add(&mut col);
        col.set_deck_review_limit(DeckId(1), 0);
        assert_eq!(col.queue_as_deck_and_template(DeckId(1)), vec![]);
    }

    #[test]
    fn new_queue_building() -> Result<()> {
        let mut col = Collection::new();

        // parent
        // ┣━━child━━grandchild
        // ┗━━child_2
        let mut parent = DeckAdder::new("parent").add(&mut col);
        let mut child = DeckAdder::new("parent::child").add(&mut col);
        let child_2 = DeckAdder::new("parent::child_2").add(&mut col);
        let grandchild = DeckAdder::new("parent::child::grandchild").add(&mut col);

        // add 2 new cards to each deck
        for deck in [&parent, &child, &child_2, &grandchild] {
            CardAdder::new().siblings(2).deck(deck.id).add(&mut col);
        }

        // set child's new limit to 3, which should affect grandchild
        col.set_deck_new_limit(&mut child, 3);

        // depth-first tree order
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::Deck);
        let cards = vec![
            (parent.id, 0),
            (parent.id, 1),
            (child.id, 0),
            (child.id, 1),
            (grandchild.id, 0),
            (child_2.id, 0),
            (child_2.id, 1),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        // insertion order
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::LowestPosition);
        let cards = vec![
            (parent.id, 0),
            (parent.id, 1),
            (child.id, 0),
            (child.id, 1),
            (child_2.id, 0),
            (child_2.id, 1),
            (grandchild.id, 0),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        // inverted insertion order, but sibling order is preserved
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::HighestPosition);
        let cards = vec![
            (grandchild.id, 0),
            (grandchild.id, 1),
            (child_2.id, 0),
            (child_2.id, 1),
            (child.id, 0),
            (parent.id, 0),
            (parent.id, 1),
        ];
        assert_eq!(col.queue_as_deck_and_template(parent.id), cards);

        Ok(())
    }

    #[test]
    fn review_queue_building() -> Result<()> {
        let mut col = Collection::new();

        let mut deck = col.get_or_create_normal_deck("Default").unwrap();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut cards = vec![];

        // relative overdueness
        let expected_queue = vec![
            (-150, 1),
            (-100, 1),
            (-50, 1),
            (-150, 5),
            (-100, 5),
            (-50, 5),
            (-150, 20),
            (-150, 20),
            (-100, 20),
            (-50, 20),
            (-150, 100),
            (-100, 100),
            (-50, 100),
            (0, 1),
            (0, 5),
            (0, 20),
            (0, 100),
        ];
        for t in expected_queue.iter() {
            let mut note = nt.new_note();
            note.set_field(0, "foo")?;
            note.id.0 = 0;
            col.add_note(&mut note, deck.id)?;
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            card.interval = t.1;
            card.due = t.0;
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            cards.push(card);
        }
        col.update_cards_maybe_undoable(cards, false)?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RelativeOverdueness);
        assert_eq!(col.queue_as_due_and_ivl(deck.id), expected_queue);

        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_ignores_stale_decay() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        col.set_deck_fsrs7_defaults(deck.id);

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, deck.id)?;
        col.add_note(&mut note2, deck.id)?;

        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();
        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 0;
            card.interval = 20;
            card.memory_state = Some(FsrsMemoryState {
                stability: 30.0,
                stability_internal: 30.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.desired_retention = Some(0.8);
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        card1.decay = Some(0.1);
        card2.decay = Some(2.0);
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;

        // Exact FSRS ordering should tie on identical state/elapsed/DR and use
        // the normal id/mtime hash tiebreaker, not stale per-card decay.
        let first_queue = col.queue_as_ids(deck.id);
        assert_eq!(first_queue.len(), 2);
        assert!(first_queue.contains(&card1.id));
        assert!(first_queue.contains(&card2.id));
        assert_eq!(col.queue_as_ids(deck.id), first_queue);
        Ok(())
    }

    /// An RWKV-Curve deck with review order `order`, desired retention 0.9,
    /// and one due review card per (interval, days since last review, FSRS
    /// stability).
    fn rwkv_curve_deck(
        col: &mut Collection,
        order: ReviewCardOrder,
        cards: &[(u32, i64, f32)],
    ) -> Result<(DeckId, Vec<CardId>)> {
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("RWKV")?;
        let mut conf = DeckConfig::default();
        conf.inner.review_order = order as i32;
        conf.inner.rwkv_review_enabled = true;
        conf.inner.desired_retention = 0.9;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut()?.config_id = conf.id.0;
        col.add_or_update_deck(&mut deck)?;
        let today = col.timing_today()?.days_elapsed as i32;
        let mut ids = Vec::new();
        for &(interval, elapsed_days, stability) in cards {
            let id = add_memory_state_card(
                col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                today,
                elapsed_days * 86_400,
                stability,
            )?;
            let mut card = col.storage.get_card(id)?.unwrap();
            card.interval = interval;
            card.desired_retention = None;
            col.storage.update_card(&card)?;
            ids.push(id);
        }
        Ok((deck.id, ids))
    }

    fn set_rwkv_curve_score(col: &mut Collection, card_id: CardId, retrievability: f32) {
        col.set_rwkv_stats_graph_score_entries(
            String::new(),
            HashMap::from([(
                card_id,
                crate::collection::RwkvStatsGraphScoreEntry {
                    retrievability: Some(retrievability),
                    curve_retrievability: Some(retrievability),
                    intervening_reviews: None,
                    target_retention: None,
                    curve_due: true,
                },
            )]),
        )
        .unwrap();
    }

    // Pins spec/scheduling.md#sched.rwkv-review-order: RWKV-Curve's curve
    // retrievability over the target retention ranks a scored card; an
    // unscored card uses the exponential curve through its RWKV interval; the
    // FSRS memory state plays no part.
    #[test]
    fn rwkv_curve_relative_overdueness_uses_rwkv_not_fsrs() -> Result<()> {
        let mut col = Collection::new();
        let (deck_id, ids) = rwkv_curve_deck(
            &mut col,
            ReviewCardOrder::RelativeOverdueness,
            &[
                // unscored, twice its interval: 0.9^(2 - 1) = 0.9
                (10, 20, 100.0),
                // scored 0.5: 0.5 / 0.9 = 0.56
                (10, 11, 100.0),
                // unscored, due exactly: 0.9^0 = 1; its tiny FSRS stability
                // would put it first under FSRS
                (10, 10, 0.01),
            ],
        )?;
        set_rwkv_curve_score(&mut col, ids[1], 0.5);
        assert_eq!(col.queue_as_ids(deck_id), vec![ids[1], ids[0], ids[2]]);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.rwkv-review-order: with no RWKV scores at
    // all, the order is the time since the last review over the RWKV
    // interval, most overdue first.
    #[test]
    fn rwkv_curve_relative_overdueness_without_scores_uses_the_rwkv_interval() -> Result<()> {
        let mut col = Collection::new();
        let (deck_id, ids) = rwkv_curve_deck(
            &mut col,
            ReviewCardOrder::RelativeOverdueness,
            &[(20, 30, 1.0), (4, 12, 1.0), (1, 1, 1.0), (10, 25, 1.0)],
        )?;
        // elapsed / interval: 1.5, 3, 1, 2.5
        assert_eq!(
            col.queue_as_ids(deck_id),
            vec![ids[1], ids[3], ids[0], ids[2]]
        );
        Ok(())
    }

    // Pins spec/scheduling.md#sched.rwkv-review-order: retrievability orders
    // in an RWKV-Curve deck rank by RWKV-Curve's retrievability (an unscored
    // card: 0.9^(elapsed / interval)), not by due day and not by FSRS.
    #[test]
    fn rwkv_curve_retrievability_orders_use_rwkv() -> Result<()> {
        // (interval, elapsed): unscored 0.9^2 = 0.81; scored 0.7; unscored
        // 0.9^1 = 0.9 with an FSRS stability that FSRS would rank lowest
        let cards = [(10, 20, 100.0), (10, 5, 100.0), (10, 10, 0.01)];
        for (order, expected) in [
            (ReviewCardOrder::RetrievabilityAscending, [1, 0, 2]),
            (ReviewCardOrder::RetrievabilityDescending, [2, 0, 1]),
        ] {
            let mut col = Collection::new();
            let (deck_id, ids) = rwkv_curve_deck(&mut col, order, &cards)?;
            set_rwkv_curve_score(&mut col, ids[1], 0.7);
            assert_eq!(
                col.queue_as_ids(deck_id),
                expected.map(|index| ids[index]).to_vec(),
                "{order:?}"
            );
        }
        Ok(())
    }

    fn add_memory_state_card(
        col: &mut Collection,
        deck_id: DeckId,
        queue: CardQueue,
        ctype: CardType,
        due: i32,
        elapsed_secs: i64,
        stability: f32,
    ) -> Result<CardId> {
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "foo")?;
        col.add_note(&mut note, deck_id)?;
        let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
        card.ctype = ctype;
        card.queue = queue;
        card.due = due;
        card.interval = 1;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.desired_retention = Some(0.9);
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-elapsed_secs));
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    fn fnvhash_card_and_mod(card: &Card) -> i64 {
        let mut hasher = FnvHasher::default();
        hasher.write_i64(card.id.0);
        hasher.write_i64(card.mtime.0);
        hasher.finish() as i64
    }

    #[test]
    fn fsrs_retrievability_order_interleaves_due_non_new_queues() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let day_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::DayLearn,
            CardType::Relearn,
            timing.days_elapsed as i32,
            4 * 86_400,
            30.0,
        )?;
        let intraday_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 - 1) as i32,
            6 * 86_400,
            30.0,
        )?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![intraday_learning, day_learning, review]
        );
        assert_eq!(col.counts(), [0, 2, 1]);
        Ok(())
    }

    #[test]
    fn fsrs_descending_retrievability_order_interleaves_due_non_new_queues() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityDescending);

        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let day_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::DayLearn,
            CardType::Relearn,
            timing.days_elapsed as i32,
            4 * 86_400,
            30.0,
        )?;
        let intraday_learning = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 - 1) as i32,
            6 * 86_400,
            30.0,
        )?;

        // the due intraday learning card comes first, by its due time; the
        // retrievability order ranks the other two
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![intraday_learning, review, day_learning]
        );
        assert_eq!(col.counts(), [0, 2, 1]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_uses_both_stabilities_and_difficulty() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        col.set_deck_fsrs7_defaults(deck.id);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            10.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            10.0,
        )?;

        let first_card = col.storage.get_card(first)?.unwrap();
        let second_card = col.storage.get_card(second)?.unwrap();
        let state_a = FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: Some(20.0),
            difficulty: 5.0,
        };
        let state_b = FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: Some(5.0),
            difficulty: 8.0,
        };
        let key_a = col.fsrs_current_retrievability_for_card_state(first, state_a, 20.0)?;
        let key_b = col.fsrs_current_retrievability_for_card_state(first, state_b, 20.0)?;
        assert_ne!(key_a, key_b);
        let (low_state, high_state) = if key_a < key_b {
            (state_a, state_b)
        } else {
            (state_b, state_a)
        };

        // Put the lower-R state second in the scalar helper's hash tie order,
        // so this test would fail if only slow stability were considered.
        let (high_r_id, low_r_id) =
            if fnvhash_card_and_mod(&first_card) < fnvhash_card_and_mod(&second_card) {
                (first, second)
            } else {
                (second, first)
            };

        let mut high_r = col.storage.get_card(high_r_id)?.unwrap();
        high_r.memory_state = Some(high_state);
        col.storage.update_card(&high_r)?;

        let mut low_r = col.storage.get_card(low_r_id)?.unwrap();
        low_r.memory_state = Some(low_state);
        col.storage.update_card(&low_r)?;

        let high_key = col.fsrs_current_retrievability_for_card_state(
            high_r.id,
            high_r.memory_state.unwrap(),
            20.0,
        )?;
        let low_key = col.fsrs_current_retrievability_for_card_state(
            low_r.id,
            low_r.memory_state.unwrap(),
            20.0,
        )?;
        assert!(low_key < high_key);
        assert_eq!(col.queue_as_ids(deck.id), vec![low_r_id, high_r_id]);
        Ok(())
    }

    #[test]
    fn fsrs_relative_overdueness_uses_the_card_target_interval() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RelativeOverdueness);
        col.set_deck_fsrs7_defaults(deck.id);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            10.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            10.0,
        )?;
        let first_card = col.storage.get_card(first)?.unwrap();
        let second_card = col.storage.get_card(second)?.unwrap();
        let (low_hash_id, high_hash_id) =
            if fnvhash_card_and_mod(&first_card) < fnvhash_card_and_mod(&second_card) {
                (first, second)
            } else {
                (second, first)
            };

        let state = first_card.memory_state.unwrap();
        let mut target_a_card = first_card.clone();
        target_a_card.desired_retention = Some(0.8);
        let target_a_key =
            col.fsrs_relative_overdueness_for_card_state(&target_a_card, state, 20.0)?;
        let mut target_b_card = first_card;
        target_b_card.desired_retention = Some(0.95);
        let target_b_key =
            col.fsrs_relative_overdueness_for_card_state(&target_b_card, state, 20.0)?;
        assert_ne!(target_a_key, target_b_key);
        let (lower_key_target, higher_key_target) = if target_a_key < target_b_key {
            (0.8, 0.95)
        } else {
            (0.95, 0.8)
        };

        // Exact R is identical for both cards, so assign the lower relative-
        // overdueness key against the normal hash order to catch an R-only sort.
        let mut low_hash_card = col.storage.get_card(low_hash_id)?.unwrap();
        low_hash_card.desired_retention = Some(higher_key_target);
        col.storage.update_card(&low_hash_card)?;
        let mut high_hash_card = col.storage.get_card(high_hash_id)?.unwrap();
        high_hash_card.desired_retention = Some(lower_key_target);
        col.storage.update_card(&high_hash_card)?;

        assert_eq!(col.queue_as_ids(deck.id), vec![high_hash_id, low_hash_id]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_applies_limits_after_sorting_filtered_child_cards() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let study = DeckAdder::new("Parent::Study").add(&mut col);
        let filtered = DeckAdder::new("Parent::Filtered")
            .filtered(true)
            .add(&mut col);
        col.set_deck_review_order(&mut parent, ReviewCardOrder::RetrievabilityAscending);
        col.set_deck_review_limit(parent.id, 1);

        let timing = col.timing_today()?;
        let lower_retrievability_card = add_memory_state_card(
            &mut col,
            study.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            30.0,
        )?;
        let filtered_card = add_memory_state_card(
            &mut col,
            study.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            30.0,
        )?;

        let mut card = col.storage.get_card(filtered_card)?.unwrap();
        card.original_deck_id = card.deck_id;
        card.deck_id = filtered.id;
        card.original_due = card.due;
        card.due = -100_000;
        col.storage.update_card(&card)?;

        assert_eq!(col.queue_as_ids(parent.id), vec![lower_retrievability_card]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_uses_actual_r_across_child_decks() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;

        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let low_r_deck = DeckAdder::new("Parent::LowR").add(&mut col);
        let high_r_deck = DeckAdder::new("Parent::HighR").add(&mut col);
        col.set_deck_review_order(&mut parent, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let low_r_card = add_memory_state_card(
            &mut col,
            low_r_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            30.0,
        )?;
        let high_r_card = add_memory_state_card(
            &mut col,
            high_r_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            30.0,
        )?;

        let mut low_r = col.storage.get_card(low_r_card)?.unwrap();
        low_r.desired_retention = Some(0.1);
        col.storage.update_card(&low_r)?;
        let mut high_r = col.storage.get_card(high_r_card)?.unwrap();
        high_r.desired_retention = Some(0.9999);
        col.storage.update_card(&high_r)?;

        let low_retrievability =
            col.fsrs_current_retrievability_for_card(low_r_card, 30.0, 20.0)?;
        let high_retrievability =
            col.fsrs_current_retrievability_for_card(high_r_card, 30.0, 1.0)?;
        assert!(low_retrievability < high_retrievability);
        assert_eq!(col.queue_as_ids(parent.id), vec![low_r_card, high_r_card]);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-review-order: a relearning card
    // due within the learn-ahead limit is shown after the due cards and
    // counted, as in the other review orders.
    #[test]
    fn fsrs_retrievability_order_keeps_learn_ahead() -> Result<()> {
        for order in [
            ReviewCardOrder::RetrievabilityAscending,
            ReviewCardOrder::RetrievabilityDescending,
            ReviewCardOrder::RelativeOverdueness,
        ] {
            let mut col = Collection::new();
            col.set_config_bool(BoolKey::Fsrs, true, true)?;
            let mut deck = col.get_or_create_normal_deck("Default")?;
            col.set_deck_review_order(&mut deck, order);

            let timing = col.timing_today()?;
            let review = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32,
                2 * 86_400,
                30.0,
            )?;
            let relearning = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Learn,
                CardType::Relearn,
                (timing.now.0 + 60) as i32,
                6 * 86_400,
                30.0,
            )?;

            assert_eq!(
                col.queue_as_ids(deck.id),
                vec![review, relearning],
                "{order:?}"
            );
            assert_eq!(col.counts(), [0, 1, 1], "{order:?}");
            col.set_current_deck(deck.id)?;
            col.answer_good();
            // with the review done, the relearning card is next, learnt ahead
            assert_eq!(col.queued_card_ids(1)?, vec![relearning], "{order:?}");
            assert_eq!(col.counts(), [0, 1, 0], "{order:?}");
        }
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-review-order: due intraday learning
    // cards come by due time, before the ranked cards, not by retrievability.
    #[test]
    fn fsrs_retrievability_order_shows_due_intraday_learning_by_due_time() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            40 * 86_400,
            1.0,
        )?;
        // due first, and the most likely to be recalled
        let due_earlier = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 - 120) as i32,
            600,
            100.0,
        )?;
        let due_later = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 - 60) as i32,
            20 * 86_400,
            1.0,
        )?;
        for card_id in [due_earlier, due_later] {
            let mut card = col.storage.get_card(card_id)?.unwrap();
            card.reps = 1;
            col.storage.update_card(&card)?;
        }

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![due_earlier, due_later, review]
        );
        assert_eq!(col.counts(), [0, 2, 1]);
        Ok(())
    }

    // RWKV-Curve shares the queue: its retrievability orders keep learn-ahead
    // too (spec sched.rwkv-review-order).
    #[test]
    fn rwkv_curve_retrievability_order_keeps_learn_ahead() -> Result<()> {
        let mut col = Collection::new();
        let (deck_id, ids) = rwkv_curve_deck(
            &mut col,
            ReviewCardOrder::RetrievabilityDescending,
            &[(10, 10, 10.0)],
        )?;
        let timing = col.timing_today()?;
        let relearning = add_memory_state_card(
            &mut col,
            deck_id,
            CardQueue::Learn,
            CardType::Relearn,
            (timing.now.0 + 60) as i32,
            6 * 86_400,
            30.0,
        )?;
        assert_eq!(col.queue_as_ids(deck_id), vec![ids[0], relearning]);
        col.set_current_deck(deck_id)?;
        assert_eq!(col.counts(), [0, 1, 1]);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_order_preserves_new_card_mix() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_review_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);
        let deck_config_id = deck.config_id().unwrap();
        let mut config = col.get_deck_config(deck_config_id, false)?.unwrap();
        config.inner.new_mix = ReviewMix::BeforeReviews as i32;
        col.add_or_update_deck_config(&mut config)?;

        let new_card = CardAdder::new().add(&mut col)[0].id;
        let timing = col.timing_today()?;
        let review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![new_card, review]);
        Ok(())
    }

    #[test]
    fn rwkv_descending_retrievability_gathers_new_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::DescendingRetrievability);

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        let unscored = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![second, first, unscored]);
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.new-retrievability-order-instant-only
    #[test]
    fn retrievability_gather_outside_rwkv_instant_ignores_instant_scores() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::DescendingRetrievability);
        // the same preset, but RWKV-Curve schedules
        let mut conf = col
            .get_deck_config(deck.config_id().unwrap(), false)?
            .unwrap();
        conf.inner.rwkv_review_enabled = true;
        conf.inner.rwkv_review_instant_order_enabled = false;
        col.add_or_update_deck_config(&mut conf)?;

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        let unscored = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.80)]))?;

        // RWKV-Instant's scores would put `second` first; RWKV-Curve gathers
        // by deck, in position order
        assert_eq!(col.queue_as_ids(deck.id), vec![first, second, unscored]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_gather_reuses_parent_scope_scores() -> Result<()> {
        let mut col = Collection::new();
        let parent = DeckAdder::new("Parent").add(&mut col);
        let mut child = DeckAdder::new("Parent::Child").add(&mut col);
        col.set_deck_gather_order(&mut child, NewCardGatherPriority::DescendingRetrievability);

        let first = CardAdder::new().deck(child.id).add(&mut col)[0].id;
        let second = CardAdder::new().deck(child.id).add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(
            parent.id,
            HashMap::from([(first, 0.10), (second, 0.80)]),
        )?;

        assert_eq!(col.queue_as_ids(child.id), vec![second, first]);
        Ok(())
    }

    #[test]
    fn rwkv_queue_rejects_scores_from_an_unrelated_scope() -> Result<()> {
        let mut col = Collection::new();
        let mut study_deck = DeckAdder::new("Study").add(&mut col);
        let prepared_deck = DeckAdder::new("Prepared").add(&mut col);
        col.set_deck_rwkv_instant_order(&mut study_deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let future_review = add_memory_state_card(
            &mut col,
            study_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(prepared_deck.id, HashMap::from([(future_review, 0.10)]))?;

        assert!(col.queue_as_ids(study_deck.id).is_empty());
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_gather_is_not_overridden_by_new_card_sort_order() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let mut conf = DeckConfig::default();
        // this test counts both siblings in the queue, and was written before
        // siblings were buried by default (spec deck-options.new-preset-defaults)
        conf.inner.bury_new = false;
        conf.inner.bury_reviews = false;
        conf.inner.bury_interday_learning = false;
        conf.inner.new_card_gather_priority =
            NewCardGatherPriority::DescendingRetrievability as i32;
        conf.inner.new_card_sort_order = NewCardSortOrder::Template as i32;
        // the retrievability gather orders are RWKV-Instant's (spec
        // deck-options.new-retrievability-order-instant-only)
        conf.inner.rwkv_review_enabled = false;
        conf.inner.rwkv_review_instant_order_enabled = true;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut().unwrap().config_id = conf.id.0;
        col.add_or_update_deck(&mut deck)?;

        let siblings = CardAdder::new().siblings(2).add(&mut col);
        let lower_template = siblings[0].id;
        let higher_template = siblings[1].id;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(lower_template, 0.61), (higher_template, 0.83)]),
        )?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![higher_template, lower_template]
        );
        Ok(())
    }

    #[test]
    fn rwkv_descending_retrievability_gathers_new_cards_across_child_decks() -> Result<()> {
        let mut col = Collection::new();
        let mut parent = DeckAdder::new("Parent").add(&mut col);
        let child1 = DeckAdder::new("Parent::Child 1").add(&mut col);
        let child2 = DeckAdder::new("Parent::Child 2").add(&mut col);
        let child3 = DeckAdder::new("Parent::Child 3").add(&mut col);
        col.set_deck_gather_order(&mut parent, NewCardGatherPriority::DescendingRetrievability);

        let card32 = CardAdder::new().deck(child1.id).add(&mut col)[0].id;
        let card33 = CardAdder::new().deck(child2.id).add(&mut col)[0].id;
        let card35 = CardAdder::new().deck(child3.id).add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(
            parent.id,
            HashMap::from([(card32, 0.32), (card33, 0.33), (card35, 0.35)]),
        )?;

        assert_eq!(col.queue_as_ids(parent.id), vec![card35, card33, card32]);
        Ok(())
    }

    #[test]
    fn rwkv_ascending_retrievability_gathers_new_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_gather_order(&mut deck, NewCardGatherPriority::AscendingRetrievability);

        let first = CardAdder::new().add(&mut col)[0].id;
        let second = CardAdder::new().add(&mut col)[0].id;
        let unscored = CardAdder::new().add(&mut col)[0].id;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![first, second, unscored]);
        Ok(())
    }

    #[test]
    fn random_reviews_resample_full_pool_before_parent_limit() -> Result<()> {
        for rwkv in [false, true] {
            let mut col = Collection::new();
            let mut parent = DeckAdder::new("Parent").add(&mut col);
            let children = [
                DeckAdder::new("Parent::A").add(&mut col),
                DeckAdder::new("Parent::B").add(&mut col),
            ];
            if rwkv {
                col.set_deck_rwkv_instant_order(&mut parent, ReviewCardOrder::Random);
            } else {
                col.set_deck_review_order(&mut parent, ReviewCardOrder::Random);
            }
            col.set_deck_review_limit(parent.id, 1);
            let today = col.timing_today()?.days_elapsed as i32;
            let mut eligible = HashSet::new();
            let mut scores = HashMap::new();
            for child in &children {
                for index in 0..4 {
                    let id = add_memory_state_card(
                        &mut col,
                        child.id,
                        CardQueue::Review,
                        CardType::Review,
                        today,
                        2 * 86_400,
                        30.0,
                    )?;
                    if !rwkv || index != 0 {
                        eligible.insert(id);
                    }
                    scores.insert(id, if index == 0 { 0.99 } else { 0.20 });
                }
            }
            if rwkv {
                col.set_rwkv_review_queue_scores(parent.id, scores)?;
            }
            let mut seen = HashSet::new();
            // No card data changes between builds: the old hash order always
            // returned the same card. The generous sample avoids flaky tests
            // without attempting to certify a statistical distribution.
            for _ in 0..256 {
                let ids = col.queue_as_ids(parent.id);
                assert_eq!(ids.len(), 1);
                assert!(eligible.contains(&ids[0]));
                seen.insert(ids[0]);
            }
            assert_eq!(seen, eligible, "RWKV enabled: {rwkv}");
        }
        Ok(())
    }

    #[test]
    fn rwkv_instant_eligibility_applies_to_every_review_order() -> Result<()> {
        for order in [
            ReviewCardOrder::Day,
            ReviewCardOrder::DayThenDeck,
            ReviewCardOrder::DeckThenDay,
            ReviewCardOrder::IntervalsAscending,
            ReviewCardOrder::IntervalsDescending,
            ReviewCardOrder::EaseAscending,
            ReviewCardOrder::EaseDescending,
            ReviewCardOrder::RetrievabilityAscending,
            ReviewCardOrder::RetrievabilityDescending,
            ReviewCardOrder::RelativeOverdueness,
            ReviewCardOrder::Random,
            ReviewCardOrder::Added,
            ReviewCardOrder::ReverseAdded,
        ] {
            let mut col = Collection::new();
            let mut deck = col.get_or_create_normal_deck("Default")?;
            col.set_deck_rwkv_instant_order(&mut deck, order);

            let timing = col.timing_today()?;
            let unscored_due = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32 - 1,
                2 * 86_400,
                30.0,
            )?;
            let scored_due_above_target = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32,
                2 * 86_400,
                30.0,
            )?;
            let scored_future_below_target = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32 + 7,
                2 * 86_400,
                30.0,
            )?;
            col.set_rwkv_review_queue_score_entries(
                deck.id,
                HashMap::from([
                    (
                        scored_due_above_target,
                        RwkvReviewQueueScoreEntry {
                            retrievability: 0.80,
                            intervening_reviews: None,
                            target_retention: Some(0.75),
                        },
                    ),
                    (
                        scored_future_below_target,
                        RwkvReviewQueueScoreEntry {
                            retrievability: 0.20,
                            intervening_reviews: None,
                            target_retention: Some(0.75),
                        },
                    ),
                ]),
            )?;

            // the unscored due card waits for its score
            let _ = unscored_due;
            assert_eq!(
                col.queue_as_ids(deck.id),
                vec![scored_future_below_target],
                "review order {order:?}"
            );
            assert_eq!(col.counts(), [0, 0, 1], "review order {order:?}");
        }
        Ok(())
    }

    #[test]
    fn rwkv_relative_overdueness_uses_score_target_before_review_limit() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RelativeOverdueness,
            0.90,
        );
        col.set_deck_review_limit(deck.id, 1);

        let timing = col.timing_today()?;
        let higher_target = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let lower_target = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            20 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([
                (
                    higher_target,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.90,
                        intervening_reviews: None,
                        target_retention: Some(0.95),
                    },
                ),
                (
                    lower_target,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.79,
                        intervening_reviews: None,
                        target_retention: Some(0.80),
                    },
                ),
            ]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![higher_target]);
        Ok(())
    }

    #[test]
    fn rwkv_instant_preserves_due_day_review_order() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::Day);

        let timing = col.timing_today()?;
        let scored_earlier = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 2,
            2 * 86_400,
            30.0,
        )?;
        let unscored_due = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 1,
            2 * 86_400,
            30.0,
        )?;
        let scored_future = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([
                (
                    scored_earlier,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.30,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    scored_future,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.20,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
            ]),
        )?;

        // due-day order, not RWKV's lower R first; the unscored due card waits
        // for its score (spec sched.rwkv-instant-waits)
        let _ = unscored_due;
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![scored_earlier, scored_future]
        );
        Ok(())
    }

    #[test]
    fn answered_card_score_patch_preserves_retained_queue() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let queued_card = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let answered_card = CardId(9_999_999_999);
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([
                (
                    answered_card,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.20,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    queued_card,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.20,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
            ]),
        )?;
        col.get_queued_cards(1, false, true)?;
        let build_time = col.state.card_queues.as_ref().unwrap().build_time;

        col.patch_answered_card_rwkv_review_queue_score_entry(
            deck.id,
            answered_card,
            Some(RwkvReviewQueueScoreEntry {
                retrievability: 0.80,
                intervening_reviews: Some(1),
                target_retention: Some(0.75),
            }),
        )?;

        assert_eq!(
            col.state.card_queues.as_ref().unwrap().build_time,
            build_time
        );
        let scores = col
            .rwkv_review_queue_scores(deck.id, timing.days_elapsed)
            .unwrap();
        assert_eq!(scores[&answered_card].retrievability, 0.80);
        assert!(col
            .patch_answered_card_rwkv_review_queue_score_entry(
                deck.id,
                queued_card,
                Some(RwkvReviewQueueScoreEntry::new(0.50)),
            )
            .is_err());
        Ok(())
    }

    #[test]
    fn eligible_answered_card_score_patch_invalidates_retained_queue() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_options(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            true,
        );
        let timing = col.timing_today()?;
        let answered_card = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let remaining_card = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(answered_card, 0.10), (remaining_card, 0.20)]),
        )?;
        assert_eq!(col.queued_card_ids(1)?, vec![answered_card]);
        col.state
            .card_queues
            .as_mut()
            .unwrap()
            .pop_entry(answered_card)?;
        let mut card = col.storage.get_card(answered_card)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;

        col.patch_answered_card_rwkv_review_queue_score_entry(
            deck.id,
            answered_card,
            Some(RwkvReviewQueueScoreEntry {
                retrievability: 0.10,
                intervening_reviews: Some(0),
                target_retention: Some(0.75),
            }),
        )?;

        assert!(col.state.card_queues.is_none());
        Ok(())
    }

    #[test]
    fn deferred_answered_card_patch_rebuilds_when_repeat_guard_is_ready() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            2,
            0,
        );
        let timing = col.timing_today()?;
        let answered_card = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let remaining_card = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(answered_card, 0.10), (remaining_card, 0.20)]),
        )?;
        assert_eq!(col.queued_card_ids(1)?, vec![answered_card]);
        col.state
            .card_queues
            .as_mut()
            .unwrap()
            .pop_entry(answered_card)?;
        let mut card = col.storage.get_card(answered_card)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;

        col.patch_answered_card_rwkv_review_queue_score_entry(
            deck.id,
            answered_card,
            Some(RwkvReviewQueueScoreEntry {
                retrievability: 0.10,
                intervening_reviews: Some(0),
                target_retention: Some(0.75),
            }),
        )?;
        assert!(col.state.card_queues.is_some());

        col.update_rwkv_review_queue_intervening_reviews(
            deck.id,
            HashMap::from([(answered_card, 2)]),
        )?;

        assert!(col.state.card_queues.is_none());
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_can_include_future_review_cards() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let other_deck = col.get_or_create_normal_deck("Other")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let due_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let inactive_deck_review = add_memory_state_card(
            &mut col,
            other_deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([
                (due_review, 0.90),
                (future_review, 0.10),
                (inactive_deck_review, 0.01),
            ]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![future_review, due_review]);
        assert_eq!(col.counts(), [0, 0, 2]);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.rwkv-instant-waits
    #[test]
    fn rwkv_instant_unscored_due_reviews_wait_for_their_score() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let due_review_without_score = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_review = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(future_review, 0.10)]))?;

        // FSRS-7 calls the unscored card due; RWKV-Instant has no value for it
        let _ = due_review_without_score;
        assert_eq!(col.queue_as_ids(deck.id), vec![future_review]);
        assert_eq!(col.counts(), [0, 0, 1]);
        assert!(!col.get_queued_cards(1, false, true)?.rwkv_scores_pending);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_continues_after_an_ineligible_ranked_chunk() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.90,
        );
        col.set_deck_review_limit(deck.id, 1);

        let timing = col.timing_today()?;
        let mut scores = HashMap::new();
        for index in 0..=gathering::RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE {
            let card_id = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32,
                2 * 86_400,
                30.0,
            )?;
            let in_first_chunk = index < gathering::RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE;
            scores.insert(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: if in_first_chunk { 0.10 } else { 0.20 },
                    intervening_reviews: None,
                    target_retention: Some(if in_first_chunk { 0.05 } else { 0.50 }),
                },
            );
        }
        let expected = *scores.keys().max().unwrap();
        col.set_rwkv_review_queue_score_entries(deck.id, scores)?;

        assert_eq!(col.queue_as_ids(deck.id), vec![expected]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_scores_above_desired_retention() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let due_high_rwkv_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future_low_rwkv_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        for card_id in [due_high_rwkv_r, future_low_rwkv_r] {
            let mut card = col.storage.get_card(card_id)?.unwrap();
            card.desired_retention = None;
            col.storage.update_card(&card)?;
        }
        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(due_high_rwkv_r, 0.80), (future_low_rwkv_r, 0.20)]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![future_low_rwkv_r]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_minimum_pulls_lowest_retrievability_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_minimum_reviews(deck.id, 2);

        let timing = col.timing_today()?;
        let mut scored_cards = Vec::new();
        for score in [0.90, 0.80, 0.95] {
            let card_id = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32 + 7,
                2 * 86_400,
                30.0,
            )?;
            scored_cards.push((card_id, score));
        }
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            scored_cards
                .iter()
                .map(|(card_id, score)| {
                    (
                        *card_id,
                        RwkvReviewQueueScoreEntry {
                            retrievability: *score,
                            intervening_reviews: None,
                            target_retention: Some(0.75),
                        },
                    )
                })
                .collect(),
        )?;

        scored_cards.sort_by(|(_, score_a), (_, score_b)| score_a.total_cmp(score_b));
        assert_eq!(
            col.queue_as_ids(deck.id),
            scored_cards[..2]
                .iter()
                .map(|(card_id, _)| *card_id)
                .collect::<Vec<_>>()
        );
        Ok(())
    }

    #[test]
    fn rwkv_minimum_counts_normally_eligible_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_minimum_reviews(deck.id, 2);

        let timing = col.timing_today()?;
        let mut cards = Vec::new();
        for score in [0.20, 0.80, 0.90] {
            let card_id = add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32 + 7,
                2 * 86_400,
                30.0,
            )?;
            cards.push((card_id, score));
        }
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            cards
                .iter()
                .map(|(card_id, score)| {
                    (
                        *card_id,
                        RwkvReviewQueueScoreEntry {
                            retrievability: *score,
                            intervening_reviews: None,
                            target_retention: Some(0.75),
                        },
                    )
                })
                .collect(),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![cards[0].0, cards[1].0]);
        Ok(())
    }

    #[test]
    fn rwkv_minimum_counts_reviews_moved_to_filtered_decks() -> Result<()> {
        let mut col = Collection::new();
        let mut source = DeckAdder::new("Source").add(&mut col);
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut source,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_minimum_reviews(source.id, 1);

        let mut filtered = Deck::new_filtered();
        filtered.name = NativeDeckName::from_native_str("Filtered");
        col.add_or_update_deck(&mut filtered)?;

        let timing = col.timing_today()?;
        let moved = add_memory_state_card(
            &mut col,
            source.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let mut moved_card = col.storage.get_card(moved)?.unwrap();
        moved_card.original_deck_id = moved_card.deck_id;
        moved_card.original_due = moved_card.due;
        moved_card.deck_id = filtered.id;
        moved_card.due = -100_000;
        col.storage.update_card(&moved_card)?;

        let pull_candidate = add_memory_state_card(
            &mut col,
            source.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_score_entries(
            source.id,
            HashMap::from([(
                pull_candidate,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.90,
                    intervening_reviews: None,
                    target_retention: Some(0.75),
                },
            )]),
        )?;

        assert_eq!(
            col.storage.filtered_review_counts_by_original_deck()?,
            vec![(source.id, 1)]
        );
        assert!(col.queue_as_ids(source.id).is_empty());
        Ok(())
    }

    #[test]
    fn rwkv_minimum_uses_lowest_scores_with_configured_review_order() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::Day);
        col.set_deck_rwkv_minimum_reviews(deck.id, 2);

        let timing = col.timing_today()?;
        let mut add_review_card = |due_offset: i32| -> Result<CardId> {
            add_memory_state_card(
                &mut col,
                deck.id,
                CardQueue::Review,
                CardType::Review,
                timing.days_elapsed as i32 + due_offset,
                2 * 86_400,
                30.0,
            )
        };
        let later_lowest = add_review_card(3)?;
        let earliest_highest = add_review_card(1)?;
        let earlier_second = add_review_card(2)?;

        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([
                (
                    later_lowest,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.80,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    earliest_highest,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.90,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    earlier_second,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.85,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
            ]),
        )?;

        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![earlier_second, later_lowest]
        );
        Ok(())
    }

    #[test]
    fn rwkv_minimum_does_not_bypass_same_day_guard() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_minimum_reviews(deck.id, 1);

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.80,
                    intervening_reviews: Some(0),
                    target_retention: Some(0.75),
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        Ok(())
    }

    #[test]
    fn rwkv_minimum_child_reviews_count_toward_parent_target() -> Result<()> {
        let mut col = Collection::new();
        let mut parent = DeckAdder::new("parent").add(&mut col);
        let mut child = DeckAdder::new("parent::child").add(&mut col);
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut parent,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut child,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );
        col.set_deck_rwkv_minimum_reviews(parent.id, 3);
        col.set_deck_rwkv_minimum_reviews(child.id, 2);

        let timing = col.timing_today()?;
        let usn = col.usn()?;
        col.update_deck_stats(
            timing.days_elapsed,
            usn,
            anki_proto::scheduler::UpdateStatsRequest {
                deck_id: child.id.0,
                review_delta: 1,
                ..Default::default()
            },
        )?;

        let child_first = add_memory_state_card(
            &mut col,
            child.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let parent_second = add_memory_state_card(
            &mut col,
            parent.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        let child_last = add_memory_state_card(
            &mut col,
            child.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_score_entries(
            parent.id,
            HashMap::from([
                (
                    child_first,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.80,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    parent_second,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.90,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
                (
                    child_last,
                    RwkvReviewQueueScoreEntry {
                        retrievability: 0.95,
                        intervening_reviews: None,
                        target_retention: Some(0.75),
                    },
                ),
            ]),
        )?;

        assert_eq!(
            col.queue_as_ids(parent.id),
            vec![child_first, parent_second]
        );
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_uses_card_desired_retention_override() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.desired_retention = Some(0.85);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.80)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_scores_above_card_desired_retention() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.90,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.desired_retention = Some(0.90);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.60,
                    intervening_reviews: None,
                    target_retention: Some(0.50),
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_excludes_same_day_reviews_by_default() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_desired_retention(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_can_allow_same_day_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_options(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            true,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_requires_min_intervening_reviews() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            2,
            0,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(1),
                    target_retention: None,
                },
            )]),
        )?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);

        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(2),
                    target_retention: None,
                },
            )]),
        )?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    #[test]
    fn rwkv_intervening_review_update_patches_existing_queue_score() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            2,
            0,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now);
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_score_entries(
            deck.id,
            HashMap::from([(
                card_id,
                RwkvReviewQueueScoreEntry {
                    retrievability: 0.20,
                    intervening_reviews: Some(1),
                    target_retention: None,
                },
            )]),
        )?;

        assert!(col.queued_card_ids(1)?.is_empty());
        assert!(col.state.card_queues.is_some());

        col.update_rwkv_review_queue_intervening_reviews(deck.id, HashMap::from([(card_id, 2)]))?;

        assert!(col.state.card_queues.is_none());
        assert_eq!(col.queued_card_ids(1)?, vec![card_id]);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_order_requires_min_elapsed_secs() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_review_order_with_repeat_guards(
            &mut deck,
            ReviewCardOrder::RetrievabilityAscending,
            0.75,
            0,
            300,
        );

        let timing = col.timing_today()?;
        let card_id = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let mut card = col.storage.get_card(card_id)?.unwrap();
        // Leave enough margin that a slow CI runner cannot cross the eligibility
        // boundary between storing the card and rebuilding the queue.
        card.last_review_time = Some(timing.now.adding_secs(-250));
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert!(col.queue_as_ids(deck.id).is_empty());
        assert_eq!(col.counts(), [0, 0, 0]);

        let mut card = col.storage.get_card(card_id)?.unwrap();
        card.last_review_time = Some(timing.now.adding_secs(-350));
        col.storage.update_card(&card)?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(card_id, 0.20)]))?;

        assert_eq!(col.queue_as_ids(deck.id), vec![card_id]);
        assert_eq!(col.counts(), [0, 0, 1]);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.rwkv-instant-waits
    #[test]
    fn rwkv_instant_without_scores_gathers_no_reviews_and_reports_pending() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let older_due_high_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 10,
            10 * 86_400,
            1000.0,
        )?;
        let later_due_low_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            0.1,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::new())?;

        // both cards are FSRS-7 due, but RWKV-Instant has not scored the deck:
        // no reviews, and the client learns that the scores are pending
        assert_eq!(col.queue_as_ids(deck.id), Vec::<CardId>::new());
        assert_eq!(col.counts(), [0, 0, 0]);
        assert!(col.get_queued_cards(1, false, true)?.rwkv_scores_pending);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(older_due_high_r, 0.95), (later_due_low_r, 0.10)]),
        )?;
        assert_eq!(col.queue_as_ids(deck.id), vec![later_due_low_r]);
        assert!(!col.get_queued_cards(1, false, true)?.rwkv_scores_pending);
        Ok(())
    }

    #[test]
    fn rwkv_review_without_instant_order_does_not_use_fsrs_retrievability_order() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut deck = col.get_or_create_normal_deck("Default")?;
        let mut conf = DeckConfig::default();
        conf.inner.review_order = ReviewCardOrder::RetrievabilityAscending as i32;
        conf.inner.rwkv_review_enabled = true;
        conf.inner.rwkv_review_instant_order_enabled = false;
        col.add_or_update_deck_config(&mut conf)?;
        deck.normal_mut().unwrap().config_id = conf.id.0;
        col.add_or_update_deck(&mut deck)?;

        let timing = col.timing_today()?;
        let older_due_high_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 - 10,
            10 * 86_400,
            1000.0,
        )?;
        let later_due_low_r = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            86_400,
            0.1,
        )?;

        let older_retrievability =
            col.fsrs_current_retrievability_for_card(older_due_high_r, 1000.0, 10.0)?;
        let later_retrievability =
            col.fsrs_current_retrievability_for_card(later_due_low_r, 0.1, 1.0)?;
        assert!(older_retrievability > later_retrievability);
        assert_eq!(
            col.queue_as_ids(deck.id),
            vec![older_due_high_r, later_due_low_r]
        );
        Ok(())
    }

    #[test]
    fn rwkv_score_update_rebuilds_review_queue_with_new_scores() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let future = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32 + 7,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.20)]))?;

        assert_eq!(col.queued_card_ids(10)?, vec![first, second]);

        col.set_rwkv_review_queue_scores(
            deck.id,
            HashMap::from([(first, 0.90), (second, 0.05), (future, 0.01)]),
        )?;

        assert_eq!(col.queued_card_ids(10)?, vec![future, second, first]);
        assert_eq!(col.counts(), [0, 0, 3]);
        Ok(())
    }

    #[test]
    fn rwkv_score_update_rebuilds_displayed_review_queue_head() -> Result<()> {
        let mut col = Collection::new();
        let mut deck = col.get_or_create_normal_deck("Default")?;
        col.set_current_deck(deck.id)?;
        col.set_deck_rwkv_instant_order(&mut deck, ReviewCardOrder::RetrievabilityAscending);

        let timing = col.timing_today()?;
        let first = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        let second = add_memory_state_card(
            &mut col,
            deck.id,
            CardQueue::Review,
            CardType::Review,
            timing.days_elapsed as i32,
            2 * 86_400,
            30.0,
        )?;
        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.10), (second, 0.20)]))?;

        assert_eq!(col.queued_card_ids(1)?, vec![first]);

        col.set_rwkv_review_queue_scores(deck.id, HashMap::from([(first, 0.90), (second, 0.05)]))?;

        assert_eq!(col.queued_card_ids(10)?, vec![second, first]);
        Ok(())
    }

    impl Collection {
        fn card_queue_len(&mut self) -> usize {
            self.get_queued_cards(5, false, false).unwrap().cards.len()
        }
    }

    #[test]
    fn new_card_potentially_burying_review_card() {
        let mut col = Collection::new();
        // add one new and one review card
        CardAdder::new().siblings(2).due_dates(["0"]).add(&mut col);
        // Potentially problematic config: New cards are shown first and would bury
        // review siblings. This poses a problem because we gather review cards first.
        col.update_default_deck_config(|config| {
            config.new_mix = ReviewMix::BeforeReviews as i32;
            config.bury_new = false;
            config.bury_reviews = true;
        });

        let old_queue_len = col.card_queue_len();
        col.answer_easy();
        col.clear_study_queues();

        // The number of cards in the queue must decrease by exactly 1, either because
        // no burying was performed, or the first built queue anticipated it and didn't
        // include the buried card.
        assert_eq!(col.card_queue_len(), old_queue_len - 1);
    }

    // Pins spec/scheduling.md#sched.new-cards-never-ignore-review-limit
    #[test]
    fn new_cards_never_ignore_review_limit() {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::NewCardsIgnoreReviewLimit, true, false)
            .unwrap();
        col.update_default_deck_config(|config| {
            config.reviews_per_day = 0;
        });
        CardAdder::new().add(&mut col);

        // the stored flag is ignored: the review limit caps the new card
        assert_eq!(col.card_queue_len(), 0);
    }

    #[test]
    fn reviews_dont_affect_new_limit_before_review_limit_is_reached() {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.new_per_day = 1;
        });
        CardAdder::new().siblings(2).due_dates(["0"]).add(&mut col);
        assert_eq!(col.card_queue_len(), 2);
    }

    #[test]
    fn may_apply_parent_limits() {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::ApplyAllParentLimits, true, false)
            .unwrap();
        col.update_default_deck_config(|config| {
            config.new_per_day = 0;
        });
        let child = DeckAdder::new("Default::child")
            .with_config(|_| ())
            .add(&mut col);
        CardAdder::new().deck(child.id).add(&mut col);
        col.set_current_deck(child.id).unwrap();
        assert_eq!(col.card_queue_len(), 0);
    }
}
