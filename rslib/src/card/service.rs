// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use crate::card::Card;
use crate::card::CardId;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::collection::Collection;
use crate::decks::DeckId;
use crate::error;
use crate::error::AnkiError;
use crate::error::OrInvalid;
use crate::error::OrNotFound;
use crate::notes::NoteId;
use crate::prelude::TimestampSecs;
use crate::prelude::Usn;
use crate::undo::Op;

impl crate::services::CardsService for Collection {
    fn get_card(
        &mut self,
        input: anki_proto::cards::CardId,
    ) -> error::Result<anki_proto::cards::Card> {
        let cid = input.into();

        self.storage
            .get_card(cid)
            .and_then(|opt| opt.or_not_found(cid))
            .map(Into::into)
    }

    fn update_cards(
        &mut self,
        input: anki_proto::cards::UpdateCardsRequest,
    ) -> error::Result<anki_proto::collection::OpChanges> {
        let cards = input
            .cards
            .into_iter()
            .map(TryInto::try_into)
            .collect::<error::Result<Vec<Card>, AnkiError>>()?;
        for card in &cards {
            card.validate_custom_data()?;
        }
        self.update_cards_maybe_undoable(cards, !input.skip_undo_entry)
            .map(Into::into)
    }

    fn remove_cards(
        &mut self,
        input: anki_proto::cards::RemoveCardsRequest,
    ) -> error::Result<anki_proto::collection::OpChangesWithCount> {
        self.transact(Op::EmptyCards, |col| {
            col.remove_cards_and_orphaned_notes(
                &input
                    .card_ids
                    .into_iter()
                    .map(Into::into)
                    .collect::<Vec<_>>(),
            )
        })
        .map(Into::into)
    }

    fn set_deck(
        &mut self,
        input: anki_proto::cards::SetDeckRequest,
    ) -> error::Result<anki_proto::collection::OpChangesWithCount> {
        let cids: Vec<_> = input.card_ids.into_iter().map(CardId).collect();
        let deck_id = input.deck_id.into();
        self.set_deck(&cids, deck_id).map(Into::into)
    }

    fn set_flag(
        &mut self,
        input: anki_proto::cards::SetFlagRequest,
    ) -> error::Result<anki_proto::collection::OpChangesWithCount> {
        self.set_card_flag(&to_card_ids(input.card_ids), input.flag)
            .map(Into::into)
    }
}

impl TryFrom<anki_proto::cards::Card> for Card {
    type Error = AnkiError;

    fn try_from(c: anki_proto::cards::Card) -> error::Result<Self, Self::Error> {
        let ctype = CardType::try_from(c.ctype as u8).or_invalid("invalid card type")?;
        let queue = CardQueue::try_from(c.queue as i8).or_invalid("invalid card queue")?;
        Ok(Card {
            id: CardId(c.id),
            note_id: NoteId(c.note_id),
            deck_id: DeckId(c.deck_id),
            template_idx: c.template_idx as u16,
            mtime: TimestampSecs(c.mtime_secs),
            usn: Usn(c.usn),
            ctype,
            queue,
            due: c.due,
            interval: c.interval,
            ease_factor: c.ease_factor as u16,
            reps: c.reps,
            lapses: c.lapses,
            remaining_steps: c.remaining_steps,
            original_due: c.original_due,
            original_deck_id: DeckId(c.original_deck_id),
            flags: c.flags as u8,
            original_position: c.original_position,
            memory_state: c.memory_state.map(Into::into),
            desired_retention: c.desired_retention,
            decay: c.decay,
            last_review_time: c.last_review_time_secs.map(TimestampSecs),
            custom_data: c.custom_data,
        })
    }
}

impl From<Card> for anki_proto::cards::Card {
    fn from(c: Card) -> Self {
        anki_proto::cards::Card {
            id: c.id.0,
            note_id: c.note_id.0,
            deck_id: c.deck_id.0,
            template_idx: c.template_idx as u32,
            mtime_secs: c.mtime.0,
            usn: c.usn.0,
            ctype: c.ctype as u32,
            queue: c.queue as i32,
            due: c.due,
            interval: c.interval,
            ease_factor: c.ease_factor as u32,
            reps: c.reps,
            lapses: c.lapses,
            remaining_steps: c.remaining_steps,
            original_due: c.original_due,
            original_deck_id: c.original_deck_id.0,
            flags: c.flags as u32,
            original_position: c.original_position,
            memory_state: c.memory_state.map(Into::into),
            desired_retention: c.desired_retention,
            decay: c.decay,
            last_review_time_secs: c.last_review_time.map(|t| t.0),
            custom_data: c.custom_data,
        }
    }
}

fn to_card_ids(v: Vec<i64>) -> Vec<CardId> {
    v.into_iter().map(CardId).collect()
}

impl From<anki_proto::cards::CardId> for CardId {
    fn from(cid: anki_proto::cards::CardId) -> Self {
        CardId(cid.cid)
    }
}

impl From<anki_proto::cards::FsrsMemoryState> for FsrsMemoryState {
    fn from(value: anki_proto::cards::FsrsMemoryState) -> Self {
        let stability_internal = value.stability_internal.unwrap_or(value.stability);
        FsrsMemoryState {
            stability: value.stability,
            stability_internal,
            stability_fast: value.stability_fast,
            difficulty: value.difficulty,
        }
    }
}

impl From<FsrsMemoryState> for anki_proto::cards::FsrsMemoryState {
    fn from(value: FsrsMemoryState) -> Self {
        anki_proto::cards::FsrsMemoryState {
            stability: value.stability,
            difficulty: value.difficulty,
            stability_internal: Some(value.stability_internal),
            stability_fast: value.stability_fast,
        }
    }
}

#[cfg(test)]
mod tests {
    use fsrs::MemoryState;
    use fsrs::DEFAULT_PARAMETERS;
    use fsrs::FSRS;

    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::prelude::*;
    use crate::scheduler::fsrs::memory_state::fsrs_memory_state_for_params;
    use crate::services::CardsService;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;

    #[test]
    fn set_deck_reassigns_card_to_target_deck() {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let cid = col.storage.card_ids_of_notes(&[note.id]).unwrap()[0];

        // the card starts in the default deck
        assert_eq!(
            col.storage.get_card(cid).unwrap().unwrap().deck_id,
            DeckId(1)
        );

        let target = DeckAdder::new("Target").add(&mut col);

        let out = CardsService::set_deck(
            &mut col,
            anki_proto::cards::SetDeckRequest {
                card_ids: vec![cid.0],
                deck_id: target.id.0,
            },
        )
        .unwrap();

        assert_eq!(out.count, 1, "one card was moved");
        assert_eq!(
            col.storage.get_card(cid).unwrap().unwrap().deck_id,
            target.id,
            "card now belongs to the target deck"
        );
    }

    // Pins spec/scheduling.md#sched.fsrs7-addon-stability-edit: an add-on
    // that changes only the S90 of a card gets FSRS-7 traces that give it;
    // an unchanged write and an RWKV-Curve collection keep the traces.
    #[test]
    fn an_addon_edit_of_the_s90_rebuilds_the_fsrs7_traces() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.update_default_deck_config(|config| config.rwkv_review_enabled = false);
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let mut card = col.storage.all_cards_of_note(note.id)?.pop().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 90;
        card.memory_state = Some(fsrs_memory_state_for_params(
            &DEFAULT_PARAMETERS,
            MemoryState {
                stability: 30.0,
                difficulty: 3.0,
                stability_fast: 5.0,
            },
        )?);
        col.storage.update_card(&card)?;
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let write_s90 = |col: &mut Collection, s90: f32| -> Result<FsrsMemoryState> {
            let mut proto: anki_proto::cards::Card = col.storage.get_card(card.id)?.unwrap().into();
            proto.memory_state.as_mut().unwrap().stability = s90;
            let _changes = CardsService::update_cards(
                col,
                anki_proto::cards::UpdateCardsRequest {
                    cards: vec![proto],
                    skip_undo_entry: false,
                },
            )?;
            Ok(col.storage.get_card(card.id)?.unwrap().memory_state.unwrap())
        };

        let before = col.storage.get_card(card.id)?.unwrap().memory_state.unwrap();
        let edited = write_s90(&mut col, 50.0)?;
        assert_eq!(edited.stability, 50.0);
        let traces_s90 = fsrs.interval_at_retrievability(edited.into(), 0.9);
        assert!((traces_s90 - 50.0).abs() < 0.05, "{traces_s90}");
        // the difficulty and the fast/internal ratio stay
        assert_eq!(edited.difficulty, before.difficulty);
        let ratio = |state: FsrsMemoryState| state.stability_fast.unwrap() / state.stability_internal;
        assert!((ratio(edited) - ratio(before)).abs() < 1e-3);

        // an unchanged S90 keeps the traces
        assert_eq!(write_s90(&mut col, 50.0)?, edited);

        // under RWKV-Curve the stability is RWKV's S90: the traces stay
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        let rwkv = write_s90(&mut col, 70.0)?;
        assert_eq!(rwkv.stability, 70.0);
        assert_eq!(rwkv.stability_internal, edited.stability_internal);
        assert_eq!(rwkv.stability_fast, edited.stability_fast);
        Ok(())
    }
}
