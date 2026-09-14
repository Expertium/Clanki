// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use itertools::Itertools;
use serde::Deserialize;
use serde::Serialize;
use serde_tuple::Serialize_tuple;
use tracing::debug;

use crate::card::Card;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::notes::Note;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::memory_state::FsrsSyncConflict;
use crate::serde::deserialize_int_from_number;
use crate::storage::card::data::card_data_string;
use crate::storage::card::data::CardData;
use crate::sync::collection::normal::ClientSyncState;
use crate::sync::collection::normal::NormalSyncer;
use crate::sync::collection::protocol::EmptyInput;
use crate::sync::collection::protocol::SyncProtocol;
use crate::sync::collection::start::ServerSyncState;
use crate::sync::request::IntoSyncRequest;
use crate::tags::join_tags;
use crate::tags::split_tags;

pub(in crate::sync) struct ChunkableIds {
    revlog: Vec<RevlogId>,
    cards: Vec<CardId>,
    notes: Vec<NoteId>,
}

#[derive(Serialize, Deserialize, Debug, Default)]
pub struct Chunk {
    #[serde(default)]
    pub done: bool,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub revlog: Vec<RevlogEntry>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub cards: Vec<CardEntry>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub notes: Vec<NoteEntry>,
}

#[derive(Serialize_tuple, Deserialize, Debug)]
pub struct NoteEntry {
    pub id: NoteId,
    pub guid: String,
    #[serde(rename = "mid")]
    pub ntid: NotetypeId,
    #[serde(rename = "mod")]
    pub mtime: TimestampSecs,
    pub usn: Usn,
    pub tags: String,
    pub fields: String,
    pub sfld: String, // always empty
    pub csum: String, // always empty
    pub flags: u32,
    pub data: String,
}

#[derive(Serialize_tuple, Deserialize, Debug)]
pub struct CardEntry {
    pub id: CardId,
    pub nid: NoteId,
    pub did: DeckId,
    pub ord: u16,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub mtime: TimestampSecs,
    pub usn: Usn,
    pub ctype: CardType,
    pub queue: CardQueue,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub due: i32,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub ivl: u32,
    pub factor: u16,
    pub reps: u32,
    pub lapses: u32,
    pub left: u32,
    #[serde(deserialize_with = "deserialize_int_from_number")]
    pub odue: i32,
    pub odid: DeckId,
    pub flags: u8,
    pub data: String,
}

impl NormalSyncer<'_> {
    pub(in crate::sync) async fn process_chunks_from_server(
        &mut self,
        state: &ClientSyncState,
        fsrs_conflicts: &mut HashMap<CardId, FsrsSyncConflict>,
    ) -> Result<()> {
        loop {
            let chunk = self.server.chunk(EmptyInput::request()).await?.json()?;

            debug!(
                done = chunk.done,
                cards = chunk.cards.len(),
                notes = chunk.notes.len(),
                revlog = chunk.revlog.len(),
                "received"
            );
            self.mark_remote_collection_changed(
                !chunk.cards.is_empty() || !chunk.notes.is_empty() || !chunk.revlog.is_empty(),
            );
            self.mark_remote_non_review_collection_changed(
                !chunk.cards.is_empty() || !chunk.notes.is_empty(),
            );
            for entry in &chunk.revlog {
                self.record_remote_review(entry.id);
            }

            self.progress.update(false, |p| {
                p.remote_update += chunk.cards.len() + chunk.notes.len() + chunk.revlog.len()
            })?;

            let done = chunk.done;
            self.col
                .apply_chunk(chunk, state.pending_usn, fsrs_conflicts)?;

            self.progress.check_cancelled()?;

            if done {
                return Ok(());
            }
        }
    }

    pub(in crate::sync) async fn send_chunks_to_server(
        &mut self,
        state: &ClientSyncState,
    ) -> Result<()> {
        let mut ids = self.col.get_chunkable_ids(state.pending_usn)?;

        loop {
            let chunk: Chunk = self.col.get_chunk(&mut ids, Some(state.server_usn))?;
            let done = chunk.done;

            debug!(
                done = chunk.done,
                cards = chunk.cards.len(),
                notes = chunk.notes.len(),
                revlog = chunk.revlog.len(),
                "sending"
            );

            self.progress.update(false, |p| {
                p.local_update += chunk.cards.len() + chunk.notes.len() + chunk.revlog.len()
            })?;

            self.server
                .apply_chunk(ApplyChunkRequest { chunk }.try_into_sync_request()?)
                .await?;

            self.progress.check_cancelled()?;

            if done {
                return Ok(());
            }
        }
    }
}

impl Collection {
    // Remote->local chunks
    //----------------------------------------------------------------

    /// pending_usn is used to decide whether the local objects are newer.
    /// If the provided objects are not modified locally, the USN inside
    /// the individual objects is used.
    /// Cards whose local pending row conflicts with the incoming row on FSRS
    /// data or schedule are recorded in `fsrs_conflicts` for the post-sync
    /// reconcile pass; the server passes a throwaway map.
    pub(in crate::sync) fn apply_chunk(
        &mut self,
        chunk: Chunk,
        pending_usn: Usn,
        fsrs_conflicts: &mut HashMap<CardId, FsrsSyncConflict>,
    ) -> Result<()> {
        self.merge_revlog(chunk.revlog)?;
        self.merge_cards(chunk.cards, pending_usn, fsrs_conflicts)?;
        self.merge_notes(chunk.notes, pending_usn)
    }

    fn merge_revlog(&self, entries: Vec<RevlogEntry>) -> Result<()> {
        for entry in entries {
            self.storage.add_revlog_entry(&entry, false)?;
        }
        Ok(())
    }

    fn merge_cards(
        &self,
        entries: Vec<CardEntry>,
        pending_usn: Usn,
        fsrs_conflicts: &mut HashMap<CardId, FsrsSyncConflict>,
    ) -> Result<()> {
        for entry in entries {
            self.add_or_update_card_if_newer(entry, pending_usn, fsrs_conflicts)?;
        }
        Ok(())
    }

    fn add_or_update_card_if_newer(
        &self,
        entry: CardEntry,
        pending_usn: Usn,
        fsrs_conflicts: &mut HashMap<CardId, FsrsSyncConflict>,
    ) -> Result<()> {
        let proceed = if let Some(existing_card) = self.storage.get_card(entry.id)? {
            if existing_card.usn.is_pending_sync(pending_usn) {
                if let Some(conflict) = fsrs_sync_conflict(&existing_card, &entry) {
                    fsrs_conflicts
                        .entry(entry.id)
                        .and_modify(|existing| existing.merge(conflict))
                        .or_insert(conflict);
                }
            }
            !existing_card.usn.is_pending_sync(pending_usn) || existing_card.mtime < entry.mtime
        } else {
            true
        };
        if proceed {
            let card = entry.into();
            self.storage.add_or_update_card(&card)?;
        }
        Ok(())
    }

    fn merge_notes(&mut self, entries: Vec<NoteEntry>, pending_usn: Usn) -> Result<()> {
        for entry in entries {
            self.add_or_update_note_if_newer(entry, pending_usn)?;
        }
        Ok(())
    }

    fn add_or_update_note_if_newer(&mut self, entry: NoteEntry, pending_usn: Usn) -> Result<()> {
        let proceed = if let Some(existing_note) = self.storage.get_note(entry.id)? {
            !existing_note.usn.is_pending_sync(pending_usn) || existing_note.mtime < entry.mtime
        } else {
            true
        };
        if proceed {
            let mut note: Note = entry.into();
            let nt = self
                .get_notetype(note.notetype_id)?
                .or_invalid("note missing notetype")?;
            note.prepare_for_update(&nt, false)?;
            self.storage.add_or_update_note(&note)?;
        }
        Ok(())
    }

    // Local->remote chunks
    //----------------------------------------------------------------

    pub(in crate::sync) fn get_chunkable_ids(&self, pending_usn: Usn) -> Result<ChunkableIds> {
        Ok(ChunkableIds {
            revlog: self.storage.objects_pending_sync("revlog", pending_usn)?,
            cards: self.storage.objects_pending_sync("cards", pending_usn)?,
            notes: self.storage.objects_pending_sync("notes", pending_usn)?,
        })
    }

    /// Fetch a chunk of ids from `ids`, returning the referenced objects.
    pub(in crate::sync) fn get_chunk(
        &self,
        ids: &mut ChunkableIds,
        server_usn_if_client: Option<Usn>,
    ) -> Result<Chunk> {
        // get a bunch of IDs
        let mut limit = CHUNK_SIZE as i32;
        let mut revlog_ids = vec![];
        let mut card_ids = vec![];
        let mut note_ids = vec![];
        let mut chunk = Chunk::default();
        while limit > 0 {
            let last_limit = limit;
            if let Some(id) = ids.revlog.pop() {
                revlog_ids.push(id);
                limit -= 1;
            }
            if let Some(id) = ids.notes.pop() {
                note_ids.push(id);
                limit -= 1;
            }
            if let Some(id) = ids.cards.pop() {
                card_ids.push(id);
                limit -= 1;
            }
            if limit == last_limit {
                // all empty
                break;
            }
        }
        if limit > 0 {
            chunk.done = true;
        }

        // remove pending status
        if !self.server {
            self.storage
                .maybe_update_object_usns("revlog", &revlog_ids, server_usn_if_client)?;
            self.storage
                .maybe_update_object_usns("cards", &card_ids, server_usn_if_client)?;
            self.storage
                .maybe_update_object_usns("notes", &note_ids, server_usn_if_client)?;
        }

        // the fetch associated objects, and return
        chunk.revlog = revlog_ids
            .into_iter()
            .map(|id| {
                self.storage.get_revlog_entry(id).map(|e| {
                    let mut e = e.unwrap();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;
        chunk.cards = card_ids
            .into_iter()
            .map(|id| {
                self.storage.get_card(id).map(|e| {
                    let mut e: CardEntry = e.unwrap().into();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;
        chunk.notes = note_ids
            .into_iter()
            .map(|id| {
                self.storage.get_note(id).map(|e| {
                    let mut e: NoteEntry = e.unwrap().into();
                    e.usn = server_usn_if_client.unwrap_or(e.usn);
                    e
                })
            })
            .collect::<Result<_>>()?;

        Ok(chunk)
    }
}

/// Compare a locally modified card row with the row the server sent for it.
/// Returns what differs when at least one side carries FSRS data and the two
/// rows disagree on that data or on the schedule; None when there is nothing
/// for the post-sync reconcile pass to do. Which row wins the merge is decided
/// separately, by `mtime`.
pub(crate) fn fsrs_sync_conflict(
    existing: &Card,
    incoming: &CardEntry,
) -> Option<FsrsSyncConflict> {
    let incoming_data = CardData::from_str(&incoming.data);
    let either_has_fsrs = incoming_data.memory_state().is_some()
        || incoming_data.fsrs_desired_retention.is_some()
        || incoming_data.decay.is_some()
        || existing.memory_state.is_some()
        || existing.desired_retention.is_some()
        || existing.decay.is_some();
    if !either_has_fsrs {
        return None;
    }
    let conflict = FsrsSyncConflict {
        schedule_differs: card_schedule_differs(existing, incoming),
        memory_state_agreed: existing.memory_state == incoming_data.memory_state(),
        last_review_time_agreed: existing.last_review_time == incoming_data.last_review_time,
    };
    let differs = !conflict.memory_state_agreed
        || !conflict.last_review_time_agreed
        || conflict.schedule_differs
        || existing.desired_retention != incoming_data.fsrs_desired_retention
        || existing.decay != incoming_data.decay;
    differs.then_some(conflict)
}

/// True when the two rows disagree on the fields a post-sync reschedule would
/// rewrite. A change of deck, card type or queue alone is not a schedule
/// difference: moving a card between decks must not reschedule it.
fn card_schedule_differs(existing: &Card, incoming: &CardEntry) -> bool {
    existing.due != incoming.due
        || existing.interval != incoming.ivl
        || existing.original_due != incoming.odue
}

impl From<CardEntry> for Card {
    fn from(e: CardEntry) -> Self {
        let data = CardData::from_str(&e.data);
        Card {
            id: e.id,
            note_id: e.nid,
            deck_id: e.did,
            template_idx: e.ord,
            mtime: e.mtime,
            usn: e.usn,
            ctype: e.ctype,
            queue: e.queue,
            due: e.due,
            interval: e.ivl,
            ease_factor: e.factor,
            reps: e.reps,
            lapses: e.lapses,
            remaining_steps: e.left,
            original_due: e.odue,
            original_deck_id: e.odid,
            flags: e.flags,
            original_position: data.original_position,
            memory_state: data.memory_state(),
            desired_retention: data.fsrs_desired_retention,
            decay: data.decay,
            last_review_time: data.last_review_time,
            custom_data: data.custom_data,
        }
    }
}

impl From<Card> for CardEntry {
    fn from(e: Card) -> Self {
        CardEntry {
            id: e.id,
            nid: e.note_id,
            did: e.deck_id,
            ord: e.template_idx,
            mtime: e.mtime,
            usn: e.usn,
            ctype: e.ctype,
            queue: e.queue,
            due: e.due,
            ivl: e.interval,
            factor: e.ease_factor,
            reps: e.reps,
            lapses: e.lapses,
            left: e.remaining_steps,
            odue: e.original_due,
            odid: e.original_deck_id,
            flags: e.flags,
            data: card_data_string(&e),
        }
    }
}

impl From<NoteEntry> for Note {
    fn from(e: NoteEntry) -> Self {
        let fields = e.fields.split('\x1f').map(ToString::to_string).collect();
        Note::new_from_storage(
            e.id,
            e.guid,
            e.ntid,
            e.mtime,
            e.usn,
            split_tags(&e.tags).map(ToString::to_string).collect(),
            fields,
            None,
            None,
        )
    }
}

impl From<Note> for NoteEntry {
    fn from(e: Note) -> Self {
        NoteEntry {
            id: e.id,
            fields: e.fields().iter().join("\x1f"),
            guid: e.guid,
            ntid: e.notetype_id,
            mtime: e.mtime,
            usn: e.usn,
            tags: join_tags(&e.tags),
            sfld: String::new(),
            csum: String::new(),
            flags: 0,
            data: String::new(),
        }
    }
}

pub fn server_chunk(col: &mut Collection, state: &mut ServerSyncState) -> Result<Chunk> {
    if state.server_chunk_ids.is_none() {
        state.server_chunk_ids = Some(col.get_chunkable_ids(state.client_usn)?);
    }
    col.get_chunk(state.server_chunk_ids.as_mut().unwrap(), None)
}

pub fn server_apply_chunk(
    req: ApplyChunkRequest,
    col: &mut Collection,
    state: &mut ServerSyncState,
) -> Result<()> {
    col.apply_chunk(req.chunk, state.client_usn, &mut HashMap::new())
}

impl Usn {
    pub(crate) fn is_pending_sync(self, pending_usn: Usn) -> bool {
        if pending_usn.0 == -1 {
            self.0 == -1
        } else {
            self.0 >= pending_usn.0
        }
    }
}

pub const CHUNK_SIZE: usize = 250;

#[derive(Serialize, Deserialize, Debug)]
pub struct ApplyChunkRequest {
    pub chunk: Chunk,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::FsrsMemoryState;

    fn review_card() -> Card {
        Card {
            id: CardId(1),
            deck_id: DeckId(1),
            ctype: CardType::Review,
            queue: CardQueue::Review,
            due: 100,
            interval: 10,
            memory_state: Some(FsrsMemoryState {
                stability: 10.0,
                stability_internal: 10.0,
                stability_fast: None,
                difficulty: 5.0,
            }),
            desired_retention: Some(0.9),
            decay: Some(0.2),
            last_review_time: Some(TimestampSecs(1_000)),
            ..Default::default()
        }
    }

    #[test]
    fn fsrs_sync_conflict_ignores_a_card_that_only_moved_deck() {
        let existing = review_card();
        let mut incoming: CardEntry = review_card().into();
        incoming.did = DeckId(2);
        assert_eq!(fsrs_sync_conflict(&existing, &incoming), None);

        // a deck move alongside an FSRS difference is flagged, but not as a
        // schedule conflict
        incoming.data = card_data_string(&Card {
            desired_retention: Some(0.8),
            ..review_card()
        });
        assert_eq!(
            fsrs_sync_conflict(&existing, &incoming),
            Some(FsrsSyncConflict {
                schedule_differs: false,
                memory_state_agreed: true,
                last_review_time_agreed: true,
            })
        );
    }

    #[test]
    fn fsrs_sync_conflict_ignores_type_and_queue_changes_alone() {
        let existing = review_card();
        let mut incoming: CardEntry = review_card().into();
        incoming.queue = CardQueue::Suspended;
        assert_eq!(fsrs_sync_conflict(&existing, &incoming), None);
        incoming.ctype = CardType::Relearn;
        assert_eq!(fsrs_sync_conflict(&existing, &incoming), None);
    }

    #[test]
    fn fsrs_sync_conflict_flags_schedule_and_memory_state_differences() {
        let existing = review_card();
        let mut incoming: CardEntry = review_card().into();
        incoming.due += 1;
        assert_eq!(
            fsrs_sync_conflict(&existing, &incoming),
            Some(FsrsSyncConflict {
                schedule_differs: true,
                memory_state_agreed: true,
                last_review_time_agreed: true,
            })
        );

        let mut incoming: CardEntry = review_card().into();
        incoming.ivl += 1;
        assert!(
            fsrs_sync_conflict(&existing, &incoming)
                .unwrap()
                .schedule_differs
        );

        let mut incoming: CardEntry = review_card().into();
        incoming.odue = 5;
        assert!(
            fsrs_sync_conflict(&existing, &incoming)
                .unwrap()
                .schedule_differs
        );

        let incoming: CardEntry = Card {
            memory_state: None,
            last_review_time: Some(TimestampSecs(2_000)),
            ..review_card()
        }
        .into();
        assert_eq!(
            fsrs_sync_conflict(&existing, &incoming),
            Some(FsrsSyncConflict {
                schedule_differs: false,
                memory_state_agreed: false,
                last_review_time_agreed: false,
            })
        );
    }

    #[test]
    fn fsrs_sync_conflict_is_none_without_fsrs_data_on_either_side() {
        let existing = Card {
            memory_state: None,
            desired_retention: None,
            decay: None,
            ..review_card()
        };
        let mut incoming: CardEntry = existing.clone().into();
        incoming.due += 1;
        incoming.ivl += 1;
        assert_eq!(fsrs_sync_conflict(&existing, &incoming), None);
    }

    #[test]
    fn fsrs_sync_conflict_merge_keeps_the_worse_of_both_observations() {
        let mut first = FsrsSyncConflict {
            schedule_differs: false,
            memory_state_agreed: true,
            last_review_time_agreed: false,
        };
        first.merge(FsrsSyncConflict {
            schedule_differs: true,
            memory_state_agreed: false,
            last_review_time_agreed: true,
        });
        assert_eq!(
            first,
            FsrsSyncConflict {
                schedule_differs: true,
                memory_state_agreed: false,
                last_review_time_agreed: false,
            }
        );
    }
}
