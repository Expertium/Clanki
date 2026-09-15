// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::sync::LazyLock;

use rand::distr::Distribution;
use rand::distr::Uniform;
use regex::Regex;

use super::answering::CardAnswer;
use crate::card::Card;
use crate::card::CardId;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::collection::Collection;
use crate::config::StringKey;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::error::Result;
use crate::prelude::*;
use crate::scheduler::timing::is_unix_epoch_timestamp;

impl Card {
    /// Make card due in `days_from_today`.
    /// If card is not a review card, convert it into one.
    /// Review/relearning cards have their interval preserved unless
    /// `force_reset` is true.
    /// If the card has no ease factor (it's new), `ease_factor` is used.
    fn set_due_date(
        &mut self,
        today: u32,
        next_day_start: i64,
        days_from_today: u32,
        ease_factor: f32,
        force_reset: bool,
        fsrs_enabled: bool,
    ) {
        let new_due = (today + days_from_today) as i32;
        let new_interval = if fsrs_enabled {
            // Don't set the interval if the card is new or if the card has an interval of 0
            // (to prevent the interval changing if set due date is used several times in a
            // row on a new card.)
            if self.queue == CardQueue::New || self.interval == 0 {
                0
            } else if let Some(last_review_time) = self.last_review_time {
                let elapsed_days =
                    TimestampSecs(next_day_start).elapsed_days_since(last_review_time);
                elapsed_days as u32 + days_from_today
            } else {
                let due = self.original_or_current_due();
                let due_diff = if is_unix_epoch_timestamp(due) {
                    let offset = (due as i64 - next_day_start) / 86_400;
                    let due = (today as i64 + offset) as i32;
                    new_due - due
                } else {
                    new_due - due
                };
                self.interval.saturating_add_signed(due_diff)
            }
        } else if force_reset || !matches!(self.ctype, CardType::Review | CardType::Relearn) {
            days_from_today.max(1)
        } else {
            self.interval.max(1)
        };
        let ease_factor = (ease_factor * 1000.0).round() as u16;

        self.schedule_as_review(new_interval, new_due, ease_factor);
    }

    fn schedule_as_review(&mut self, interval: u32, due: i32, ease_factor: u16) {
        self.original_position = self.last_position();
        self.remove_from_filtered_deck_before_reschedule();
        self.interval = interval;
        self.due = due;
        self.ctype = CardType::Review;
        self.queue = CardQueue::Review;
        if self.ease_factor == 0 {
            // unlike the old Python code, we leave the ease factor alone
            // if it's already set
            self.ease_factor = ease_factor;
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct DueDateSpecifier {
    min: u32,
    max: u32,
    force_reset: bool,
}

pub fn parse_due_date_str(s: &str) -> Result<DueDateSpecifier> {
    static RE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"(?x)^
            # a number
            (?P<min>\d+)
            # an optional hyphen and another number
            (?:
                -
                (?P<max>\d+)
            )?
            # optional exclamation mark
            (?P<bang>!)?
            $
        ",
        )
        .unwrap()
    });
    let caps = RE.captures(s).or_invalid(s)?;
    let min: u32 = caps.name("min").unwrap().as_str().parse()?;
    let max = if let Some(max) = caps.name("max") {
        max.as_str().parse()?
    } else {
        min
    };
    let force_reset = caps.name("bang").is_some();
    Ok(DueDateSpecifier {
        min: min.min(max),
        max: max.max(min),
        force_reset,
    })
}

impl Collection {
    /// `days` should be in a format parseable by `parse_due_date_str`.
    /// If `context` is provided, provided key will be updated with the new
    /// value of `days`.
    pub fn set_due_date(
        &mut self,
        cids: &[CardId],
        days: &str,
        context: Option<StringKey>,
    ) -> Result<OpOutput<()>> {
        let spec = parse_due_date_str(days)?;
        let usn = self.usn()?;
        let today = self.timing_today()?.days_elapsed;
        let next_day_start = self.timing_today()?.next_day_at.0;
        let mut rng = rand::rng();
        let distribution = Uniform::new_inclusive(spec.min, spec.max).unwrap();
        let mut decks_initial_ease: HashMap<DeckId, f32> = HashMap::new();
        let fsrs_enabled = self.get_config_bool(BoolKey::Fsrs);
        self.transact(Op::SetDueDate, |col| {
            for mut card in col.all_cards_for_ids(cids, false)? {
                let deck_id = card.original_deck_id.or(card.deck_id);
                let ease_factor = match decks_initial_ease.get(&deck_id) {
                    Some(ease) => *ease,
                    None => {
                        let deck = col.get_deck(deck_id)?.or_not_found(deck_id)?;
                        let config_id = deck.config_id().or_invalid("home deck is filtered")?;
                        let ease = col
                            .get_deck_config(config_id, true)?
                            // just for compiler; get_deck_config() is guaranteed to return a value
                            .unwrap_or_default()
                            .inner
                            .initial_ease;
                        decks_initial_ease.insert(deck_id, ease);
                        ease
                    }
                };
                let original = card.clone();
                let days_from_today = distribution.sample(&mut rng);
                card.set_due_date(
                    today,
                    next_day_start,
                    days_from_today,
                    ease_factor,
                    spec.force_reset,
                    fsrs_enabled,
                );
                col.log_manually_scheduled_review(&card, original.interval, usn)?;
                col.update_card_inner(&mut card, original, usn)?;
            }
            if let Some(key) = context {
                col.set_config_string_inner(key, days)?;
            }
            Ok(())
        })
    }

    pub fn grade_now(
        &mut self,
        input: anki_proto::scheduler::GradeNowRequest,
    ) -> Result<OpOutput<()>> {
        let cids = input.card_ids.into_newtype(CardId);
        let mut card_options_by_id: HashMap<_, _> = input
            .card_options
            .into_iter()
            .map(|options| (CardId(options.card_id), options))
            .collect();
        let rating = input.rating;
        // Under RWKV-Curve only the caller can supply the states: FSRS-7's
        // must never stand in for RWKV-Curve's (spec sched.grade-now-rwkv-curve).
        let states_required =
            self.effective_scheduling_algorithm()? == SchedulingAlgorithm::RwkvCurve;

        self.transact(Op::GradeNow, |col| {
            for &card_id in &cids {
                let options = card_options_by_id.remove(&card_id).unwrap_or_default();
                let desired_retention_override = options.desired_retention_override;
                let mut states: anki_proto::scheduler::SchedulingStates =
                    if let Some(states) = options.scheduling_states {
                        states
                    } else {
                        require!(
                        !states_required,
                        "Grade Now under RWKV-Curve needs RWKV-Curve's states for card {card_id}"
                    );
                        col.get_scheduling_states_with_desired_retention_override(
                            card_id,
                            desired_retention_override,
                        )?
                        .into()
                    };
                let new_state = match rating {
                    0 => states.again.take(),
                    1 => states.hard.take(),
                    2 => states.good.take(),
                    3 => states.easy.take(),
                    _ => invalid_input!("invalid rating"),
                }
                .unwrap_or_default();
                let mut answer: CardAnswer = anki_proto::scheduler::CardAnswer {
                    card_id: card_id.into(),
                    current_state: states.current.take(),
                    new_state: Some(new_state),
                    rating,
                    milliseconds_taken: 0,
                    answered_at_millis: TimestampMillis::now().into(),
                    desired_retention_override,
                    rwkv_s90: options.rwkv_s90,
                    rwkv_retrievability: options.rwkv_retrievability,
                    rwkv_review_kind: options.rwkv_review_kind,
                }
                .into();
                // Process the card without updating queues yet
                answer.from_queue = false;
                col.answer_card_inner(&mut answer)?;
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::prelude::*;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::states::CardState;
    use crate::scheduler::states::NormalState;

    #[test]
    fn grade_now_desired_retention_override_is_saved() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        let mut card = col.get_first_card();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 10;
        card.due = col.timing_today()?.days_elapsed as i32;
        card.memory_state = Some(crate::card::FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-5 * 86_400));
        col.storage.update_card(&card)?;

        col.grade_now(anki_proto::scheduler::GradeNowRequest {
            card_ids: vec![card.id.into()],
            rating: anki_proto::scheduler::card_answer::Rating::Good as i32,
            card_options: vec![anki_proto::scheduler::grade_now_request::CardOptions {
                card_id: card.id.into(),
                desired_retention_override: Some(0.72),
                ..Default::default()
            }],
        })?;

        let card = col.storage.get_card(card.id)?.unwrap();
        assert_eq!(card.desired_retention, Some(0.72));

        Ok(())
    }

    fn add_due_review_card(col: &mut Collection) -> Result<CardId> {
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let mut card = col.storage.all_cards_of_note(note.id)?.remove(0);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 20;
        card.due = col.timing_today()?.days_elapsed as i32;
        card.memory_state = Some(crate::card::FsrsMemoryState {
            stability: 20.0,
            stability_internal: 20.0,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-20 * 86_400));
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    fn good_options(
        card_id: CardId,
        states: Option<anki_proto::scheduler::SchedulingStates>,
    ) -> anki_proto::scheduler::grade_now_request::CardOptions {
        anki_proto::scheduler::grade_now_request::CardOptions {
            card_id: card_id.into(),
            scheduling_states: states,
            rwkv_s90: Some(80.0),
            rwkv_retrievability: Some(0.83),
            rwkv_review_kind: Some(1),
            ..Default::default()
        }
    }

    fn grade_good(
        col: &mut Collection,
        options: anki_proto::scheduler::grade_now_request::CardOptions,
    ) -> Result<OpOutput<()>> {
        col.grade_now(anki_proto::scheduler::GradeNowRequest {
            card_ids: vec![options.card_id],
            rating: anki_proto::scheduler::card_answer::Rating::Good as i32,
            card_options: vec![options],
        })
    }

    // Pins spec/scheduling.md#sched.grade-now-rwkv-curve: under RWKV-Curve,
    // Grade Now answers with the RWKV-Curve states its caller supplies,
    // exactly as the reviewer answers with them, and refuses a card without
    // them instead of answering it with FSRS-7's.
    #[test]
    fn grade_now_under_rwkv_curve_answers_like_the_reviewer_and_never_with_fsrs7() -> Result<()> {
        let mut col = crate::collection::CollectionBuilder::default().build()?;
        assert_eq!(
            col.effective_scheduling_algorithm()?,
            SchedulingAlgorithm::RwkvCurve
        );
        let graded = add_due_review_card(&mut col)?;
        let reviewed = add_due_review_card(&mut col)?;
        let intervals = [Some(3.0), Some(33.0), Some(77.0), Some(90.0)];
        let s90s = [Some(4.0), Some(35.0), Some(80.0), Some(95.0)];

        // without RWKV-Curve's states nothing is answered: FSRS-7 would have
        // given this card its own Good interval
        let CardState::Normal(NormalState::Review(fsrs7_good)) =
            col.get_scheduling_states(graded)?.good
        else {
            panic!("expected a review state");
        };
        assert_ne!(fsrs7_good.scheduled_days, 77);
        assert!(grade_good(&mut col, good_options(graded, None)).is_err());
        assert_eq!(col.storage.get_card(graded)?.unwrap().interval, 20);
        assert!(col.storage.get_revlog_entries_for_card(graded)?.is_empty());

        // with them, the card gets RWKV-Curve's interval and S90, as the
        // reviewer's answer with the same states gives the other card
        let states = col.scheduling_states_with_intervals(graded, intervals, s90s)?;
        grade_good(&mut col, good_options(graded, Some(states.into())))?;
        assert_eq!(col.can_undo(), Some(&Op::GradeNow));
        let states = col.scheduling_states_with_intervals(reviewed, intervals, s90s)?;
        col.answer_card(&mut CardAnswer {
            card_id: reviewed,
            current_state: states.current,
            new_state: states.good,
            rating: crate::scheduler::answering::Rating::Good,
            answered_at: TimestampMillis::now(),
            milliseconds_taken: 0,
            custom_data: None,
            desired_retention_override: None,
            rwkv_s90: Some(80.0),
            rwkv_retrievability: Some(0.83),
            rwkv_review_kind: Some(1),
            from_queue: false,
        })?;

        let graded_card = col.storage.get_card(graded)?.unwrap();
        let reviewed_card = col.storage.get_card(reviewed)?.unwrap();
        assert_eq!(graded_card.interval, 77);
        assert_eq!(graded_card.memory_state.unwrap().stability, 80.0);
        assert_eq!(
            (
                graded_card.interval,
                graded_card.due,
                graded_card.queue,
                graded_card.ctype,
                graded_card.memory_state,
                graded_card.desired_retention,
            ),
            (
                reviewed_card.interval,
                reviewed_card.due,
                reviewed_card.queue,
                reviewed_card.ctype,
                reviewed_card.memory_state,
                reviewed_card.desired_retention,
            )
        );
        let graded_log = col.storage.get_revlog_entries_for_card(graded)?;
        let reviewed_log = col.storage.get_revlog_entries_for_card(reviewed)?;
        assert_eq!(graded_log.len(), 1);
        let row = |entry: &RevlogEntry| {
            (
                entry.button_chosen,
                entry.interval,
                entry.last_interval,
                entry.ease_factor,
                entry.review_kind,
            )
        };
        assert_eq!(row(&graded_log[0]), row(&reviewed_log[0]));
        assert_eq!(graded_log[0].review_kind, RevlogReviewKind::Review);
        let cached: Vec<f32> = col
            .storage
            .db
            .prepare("select prediction from search_stats_rwkv_review_retrievability")?
            .query_map([], |row| row.get(0))?
            .collect::<std::result::Result<_, _>>()?;
        assert_eq!(cached.len(), 2);
        assert!(cached.iter().all(|value| (value - 0.83).abs() < 1e-6));

        Ok(())
    }

    // Pins spec/scheduling.md#sched.grade-now-rwkv-curve: RWKV-Instant has no
    // intervals of its own; Grade Now answers with the FSRS states its
    // reviewer stores, and keeps the review kind the caller gives.
    #[test]
    fn grade_now_under_rwkv_instant_answers_with_fsrs_states() -> Result<()> {
        let mut col = crate::collection::CollectionBuilder::default().build()?;
        col.update_default_deck_config(|config| SchedulingAlgorithm::RwkvInstant.apply_to(config));
        assert_eq!(
            col.effective_scheduling_algorithm()?,
            SchedulingAlgorithm::RwkvInstant
        );
        let cid = add_due_review_card(&mut col)?;
        let CardState::Normal(NormalState::Review(fsrs_good)) =
            col.get_scheduling_states(cid)?.good
        else {
            panic!("expected a review state");
        };

        let mut options = good_options(cid, None);
        options.rwkv_s90 = None;
        options.rwkv_review_kind = Some(3);
        grade_good(&mut col, options)?;

        let card = col.storage.get_card(cid)?.unwrap();
        assert_eq!(card.interval, fsrs_good.scheduled_days);
        let log = col.storage.get_revlog_entries_for_card(cid)?;
        assert_eq!(log[0].review_kind, RevlogReviewKind::Filtered);

        Ok(())
    }

    #[test]
    fn parse() -> Result<()> {
        type S = DueDateSpecifier;
        assert!(parse_due_date_str("").is_err());
        assert!(parse_due_date_str("x").is_err());
        assert!(parse_due_date_str("-5").is_err());
        assert_eq!(
            parse_due_date_str("5")?,
            S {
                min: 5,
                max: 5,
                force_reset: false
            }
        );
        assert_eq!(
            parse_due_date_str("5!")?,
            S {
                min: 5,
                max: 5,
                force_reset: true
            }
        );
        assert_eq!(
            parse_due_date_str("50-70")?,
            S {
                min: 50,
                max: 70,
                force_reset: false
            }
        );
        assert_eq!(
            parse_due_date_str("70-50!")?,
            S {
                min: 50,
                max: 70,
                force_reset: true
            }
        );
        Ok(())
    }

    #[test]
    fn due_date() {
        let mut c = Card::new(NoteId(0), 0, DeckId(0), 0);

        // setting the due date of a new card will convert it
        c.set_due_date(5, 0, 2, 1.8, false, false);
        assert_eq!(c.ctype, CardType::Review);
        assert_eq!(c.due, 7);
        assert_eq!(c.interval, 2);
        assert_eq!(c.ease_factor, 1800);

        // reschedule it again the next day, shifting it from day 7 to day 9
        c.set_due_date(6, 0, 3, 2.5, false, false);
        assert_eq!(c.due, 9);
        assert_eq!(c.interval, 2);
        assert_eq!(c.ease_factor, 1800); // interval doesn't change

        // we can bring cards forward too - return it to its original due date
        c.set_due_date(6, 0, 1, 2.4, false, false);
        assert_eq!(c.due, 7);
        assert_eq!(c.interval, 2);
        assert_eq!(c.ease_factor, 1800); // interval doesn't change

        // we can force the interval to be reset instead of shifted
        c.set_due_date(6, 0, 3, 2.3, true, false);
        assert_eq!(c.due, 9);
        assert_eq!(c.interval, 3);
        assert_eq!(c.ease_factor, 1800); // interval doesn't change

        // should work in a filtered deck
        c.interval = 2;
        c.ease_factor = 0;
        c.original_due = 7;
        c.original_deck_id = DeckId(1);
        c.due = -10000;
        c.queue = CardQueue::New;
        c.set_due_date(6, 0, 1, 2.2, false, false);
        assert_eq!(c.due, 7);
        assert_eq!(c.interval, 2);
        assert_eq!(c.ease_factor, 2200);
        assert_eq!(c.queue, CardQueue::Review);
        assert_eq!(c.original_due, 0);
        assert_eq!(c.original_deck_id, DeckId(0));

        // relearning treated like review
        c.ctype = CardType::Relearn;
        c.original_due = c.due;
        c.due = 12345678;
        c.set_due_date(6, 0, 10, 2.1, false, false);
        assert_eq!(c.due, 16);
        assert_eq!(c.interval, 2);
        assert_eq!(c.ease_factor, 2200); // interval doesn't change
    }
}
