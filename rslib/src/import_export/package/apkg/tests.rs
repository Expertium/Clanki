// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

#![cfg(test)]

use std::collections::HashSet;
use std::fs::File;
use std::io::Write;

use anki_io::read_file;
use anki_proto::import_export::ImportAnkiPackageOptions;

use crate::card::FsrsMemoryState;
use crate::import_export::package::ExportAnkiPackageOptions;
use crate::media::files::sha1_of_data;
use crate::media::MediaManager;
use crate::prelude::*;
use crate::search::SearchNode;
use crate::tests::open_fs_test_collection;
use crate::tests::DeckAdder;
use crate::tests::NoteAdder;

const SAMPLE_JPG: &str = "sample.jpg";
const SAMPLE_MP3: &str = "sample.mp3";
const SAMPLE_JS: &str = "_sample.js";
const JPG_DATA: &[u8] = b"1";
const MP3_DATA: &[u8] = b"2";
const JS_DATA: &[u8] = b"3";
const EXISTING_MP3_DATA: &[u8] = b"4";

#[test]
fn roundtrip() {
    roundtrip_inner(true);
    roundtrip_inner(false);
}

fn roundtrip_inner(legacy: bool) {
    let (mut src_col, src_tempdir) = open_fs_test_collection("src");
    let (mut target_col, _target_tempdir) = open_fs_test_collection("target");
    let apkg_path = src_tempdir.path().join("test.apkg");

    let (main_deck, sibling_deck) = src_col.add_sample_decks();
    let notetype = src_col.add_sample_notetype();
    let note = src_col.add_sample_note(&main_deck, &sibling_deck, &notetype);
    src_col.add_sample_media();
    target_col.add_conflicting_media();

    src_col
        .export_apkg(
            &apkg_path,
            ExportAnkiPackageOptions {
                with_scheduling: true,
                with_deck_configs: true,
                with_media: true,
                legacy,
            },
            SearchNode::from_deck_name("parent::sample"),
            None,
        )
        .unwrap();
    target_col
        .import_apkg(&apkg_path, ImportAnkiPackageOptions::default())
        .unwrap();

    target_col.assert_decks();
    target_col.assert_notetype(&notetype);
    target_col.assert_note_and_media(&note);

    target_col.undo().unwrap();
    target_col.assert_empty();
}

impl Collection {
    fn add_sample_decks(&mut self) -> (Deck, Deck) {
        let sample = self.add_named_deck("parent\x1fsample");
        self.add_named_deck("parent\x1fsample\x1fchild");
        let siblings = self.add_named_deck("siblings");

        (sample, siblings)
    }

    fn add_named_deck(&mut self, name: &str) -> Deck {
        let mut deck = Deck::new_normal();
        deck.name = NativeDeckName::from_native_str(name);
        self.add_deck(&mut deck).unwrap();
        deck
    }

    fn add_sample_notetype(&mut self) -> Notetype {
        let mut nt = Notetype {
            name: "sample".into(),
            ..Default::default()
        };
        nt.add_field("sample");
        nt.add_template("sample1", "{{sample}}", "<script src=_sample.js></script>");
        nt.add_template("sample2", "{{sample}}2", "");
        self.add_notetype(&mut nt, true).unwrap();
        nt
    }

    fn add_sample_note(
        &mut self,
        main_deck: &Deck,
        sibling_decks: &Deck,
        notetype: &Notetype,
    ) -> Note {
        let mut sample = notetype.new_note();
        sample.fields_mut()[0] = format!("<img src='{SAMPLE_JPG}'> [sound:{SAMPLE_MP3}]");
        sample.tags = vec!["sample".into()];
        self.add_note(&mut sample, main_deck.id).unwrap();

        let card = self
            .storage
            .get_card_by_ordinal(sample.id, 1)
            .unwrap()
            .unwrap();
        self.set_deck(&[card.id], sibling_decks.id).unwrap();

        sample
    }

    fn add_sample_media(&self) {
        self.add_media(&[
            (SAMPLE_JPG, JPG_DATA),
            (SAMPLE_MP3, MP3_DATA),
            (SAMPLE_JS, JS_DATA),
        ]);
    }

    fn add_conflicting_media(&mut self) {
        let mut file = File::create(self.media_folder.join(SAMPLE_MP3)).unwrap();
        file.write_all(EXISTING_MP3_DATA).unwrap();
    }

    fn assert_decks(&mut self) {
        let existing_decks: HashSet<_> = self
            .get_all_deck_names(true)
            .unwrap()
            .into_iter()
            .map(|(_, name)| name)
            .collect();
        for deck in ["parent", "parent::sample", "siblings"] {
            assert!(existing_decks.contains(deck));
        }
        assert!(!existing_decks.contains("parent::sample::child"));
    }

    fn assert_notetype(&mut self, notetype: &Notetype) {
        assert!(self.get_notetype(notetype.id).unwrap().is_some());
    }

    fn assert_note_and_media(&mut self, note: &Note) {
        let sha1 = sha1_of_data(MP3_DATA);
        let new_mp3_name = format!("sample-{}.mp3", hex::encode(sha1));
        let csums = MediaManager::new(&self.media_folder, &self.media_db)
            .unwrap()
            .all_checksums_as_is();

        for (fname, orig_data) in [
            (SAMPLE_JPG, JPG_DATA),
            (SAMPLE_MP3, EXISTING_MP3_DATA),
            (new_mp3_name.as_str(), MP3_DATA),
            (SAMPLE_JS, JS_DATA),
        ] {
            // data should have been copied correctly
            assert_eq!(read_file(self.media_folder.join(fname)).unwrap(), orig_data);
            // and checksums in media db should be valid
            assert_eq!(*csums.get(fname).unwrap(), sha1_of_data(orig_data));
        }

        let imported_note = self.storage.get_note(note.id).unwrap().unwrap();
        assert!(imported_note.fields()[0].contains(&new_mp3_name));
    }

    fn assert_empty(&self) {
        assert!(self.get_all_deck_names(true).unwrap().is_empty());
        assert!(self.storage.get_all_note_ids().unwrap().is_empty());
        assert!(self.storage.get_all_card_ids().unwrap().is_empty());
        assert!(self.storage.all_tags().unwrap().is_empty());
    }
}

fn export_and_reimport_with_fsrs_params(with_scheduling: bool) -> DeckConfig {
    let (mut src_col, src_tempdir) = open_fs_test_collection("src");
    let (mut target_col, _target_tempdir) = open_fs_test_collection("target");
    let apkg_path = src_tempdir.path().join("fsrs.apkg");

    let deck = DeckAdder::new("fsrs-deck")
        .with_config(|c| {
            c.name = "fsrs-config".into();
            c.inner.fsrs_params_4 = vec![0.1; 17];
            c.inner.fsrs_params_5 = vec![0.2; 19];
            c.inner.fsrs_params_6 = vec![0.3; 21];
        })
        .add(&mut src_col);
    NoteAdder::basic(&mut src_col)
        .deck(deck.id)
        .add(&mut src_col);

    src_col
        .export_apkg(
            &apkg_path,
            ExportAnkiPackageOptions {
                with_scheduling,
                with_deck_configs: true,
                with_media: false,
                legacy: false,
            },
            SearchNode::WholeCollection,
            None,
        )
        .unwrap();
    target_col
        .import_apkg(
            &apkg_path,
            ImportAnkiPackageOptions {
                with_scheduling: true,
                with_deck_configs: true,
                ..Default::default()
            },
        )
        .unwrap();

    target_col
        .storage
        .all_deck_config()
        .unwrap()
        .into_iter()
        .find(|c| c.name == "fsrs-config")
        .expect("custom config should have been imported")
}

#[test]
fn fsrs_params_preserved_on_export_with_scheduling() {
    let conf = export_and_reimport_with_fsrs_params(true);
    assert_eq!(conf.inner.fsrs_params_4.len(), 17);
    assert_eq!(conf.inner.fsrs_params_5.len(), 19);
    assert_eq!(conf.inner.fsrs_params_6.len(), 21);
}

/// Sets the data column of every card in a legacy .apkg (its collection is
/// plain SQLite) the way official Anki's export writes it; Clanki's own
/// export always adds `s_int`.
fn set_card_data_in_legacy_apkg(apkg: &std::path::Path, data: &str) {
    let mut archive = zip::ZipArchive::new(File::open(apkg).unwrap()).unwrap();
    let mut entries = Vec::new();
    for index in 0..archive.len() {
        let mut file = archive.by_index(index).unwrap();
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut file, &mut bytes).unwrap();
        entries.push((file.name().to_string(), bytes));
    }
    drop(archive);
    let dir = tempfile::tempdir().unwrap();
    let mut patched = false;
    for (name, bytes) in &mut entries {
        if name == "collection.anki2" || name == "collection.anki21" {
            let path = dir.path().join(name.as_str());
            std::fs::write(&path, &bytes).unwrap();
            let db = rusqlite::Connection::open(&path).unwrap();
            db.execute("update cards set data = ?", [data]).unwrap();
            db.close().unwrap();
            *bytes = std::fs::read(&path).unwrap();
            patched = true;
        }
    }
    assert!(patched, "no legacy collection in the package");
    let mut writer = zip::ZipWriter::new(File::create(apkg).unwrap());
    for (name, bytes) in entries {
        writer
            .start_file(name, zip::write::FileOptions::<'static, ()>::default())
            .unwrap();
        writer.write_all(&bytes).unwrap();
    }
    writer.finish().unwrap();
}

// Pins spec/sync.md#sync.fsrs7-state-of-foreign-cards: an imported card
// another client wrote (no FSRS-7 internal stability) gets an FSRS-7 memory
// state; without a review log, the one whose S90 is its stored stability,
// with its stored difficulty.
#[test]
fn imported_foreign_fsrs_state_becomes_an_fsrs7_state() {
    let (mut src_col, src_tempdir) = open_fs_test_collection("src");
    let (mut target_col, _target_tempdir) = open_fs_test_collection("target");
    target_col
        .set_config_bool(BoolKey::Fsrs, true, false)
        .unwrap();
    let apkg_path = src_tempdir.path().join("foreign.apkg");
    let note = NoteAdder::basic(&mut src_col).add(&mut src_col);
    // a review card without a review log...
    src_col
        .storage
        .db
        .execute(
            "update cards set type = 2, queue = 2, ivl = 20, due = 30 where nid = ?",
            [note.id],
        )
        .unwrap();
    src_col
        .export_apkg(
            &apkg_path,
            ExportAnkiPackageOptions {
                with_scheduling: true,
                with_deck_configs: false,
                with_media: false,
                legacy: true,
            },
            SearchNode::WholeCollection,
            None,
        )
        .unwrap();
    // ...in a package as official Anki writes it
    set_card_data_in_legacy_apkg(&apkg_path, r#"{"s":20.0,"d":6.0}"#);
    target_col
        .import_apkg(
            &apkg_path,
            ImportAnkiPackageOptions {
                with_scheduling: true,
                ..Default::default()
            },
        )
        .unwrap();

    let (card_id, data): (CardId, String) = target_col
        .storage
        .db
        .query_row("select id, data from cards", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .unwrap();
    assert!(data.contains("\"s_int\""), "{data}");
    let state = target_col
        .storage
        .get_card(card_id)
        .unwrap()
        .unwrap()
        .memory_state
        .unwrap();
    assert_eq!(state.stability, 20.0);
    assert_eq!(state.difficulty, 6.0);
    let fsrs = fsrs::FSRS::new(&fsrs::DEFAULT_PARAMETERS).unwrap();
    let s90 = fsrs.interval_at_retrievability(
        fsrs::MemoryState {
            stability: state.stability_internal,
            stability_fast: state.stability_fast.unwrap(),
            difficulty: state.difficulty,
        },
        0.9,
    );
    assert!((s90 - 20.0).abs() < 0.01, "{state:?} reaches 90% at {s90}");
}

/// A studied review card in `col` with two answers in its review log, and
/// the stored memory state `state`; its id.
fn add_studied_card(col: &mut Collection, state: Option<FsrsMemoryState>) -> CardId {
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;

    let note = NoteAdder::basic(col).add(col);
    let mut card = col.storage.all_cards_of_note(note.id).unwrap().remove(0);
    let timing = col.timing_today().unwrap();
    card.ctype = CardType::Review;
    card.queue = CardQueue::Review;
    card.interval = 15;
    card.due = timing.days_elapsed as i32 + 5;
    card.memory_state = state;
    card.last_review_time = Some(TimestampSecs(timing.now.0 - 10 * 86_400));
    col.storage.update_card(&card).unwrap();
    for (days_ago, kind, interval) in [
        (20, RevlogReviewKind::Learning, -600),
        (10, RevlogReviewKind::Review, 15),
    ] {
        let entry = RevlogEntry {
            id: RevlogId((timing.now.0 - days_ago * 86_400) * 1000),
            cid: card.id,
            button_chosen: 3,
            interval,
            ease_factor: 2500,
            review_kind: kind,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&entry, false).unwrap();
    }
    card.id
}

fn export_with_scheduling(col: &mut Collection, apkg: &std::path::Path) {
    col.export_apkg(
        apkg,
        ExportAnkiPackageOptions {
            with_scheduling: true,
            with_deck_configs: false,
            with_media: false,
            legacy: false,
        },
        SearchNode::WholeCollection,
        None,
    )
    .unwrap();
}

fn import_with_scheduling(col: &mut Collection, apkg: &std::path::Path) -> Card {
    col.import_apkg(
        apkg,
        ImportAnkiPackageOptions {
            with_scheduling: true,
            ..Default::default()
        },
    )
    .unwrap();
    let card_id: CardId = col
        .storage
        .db
        .query_row("select id from cards", [], |row| row.get(0))
        .unwrap();
    col.storage.get_card(card_id).unwrap().unwrap()
}

// Pins spec/scheduling.md#sched.apkg-import-reads-the-package: a package's
// studied card without a memory state gets its FSRS-7 memory state from its
// review log with the importing collection's preset, not the package's.
#[test]
fn an_imported_card_gets_its_memory_state_from_the_importing_preset() {
    let (mut src_col, src_tempdir) = open_fs_test_collection("src");
    let (mut target_col, _target_tempdir) = open_fs_test_collection("target");
    target_col
        .set_config_bool(BoolKey::Fsrs, true, false)
        .unwrap();
    let mut params = fsrs::DEFAULT_PARAMETERS.to_vec();
    for w in &mut params[..4] {
        *w *= 3.0;
    }
    target_col.update_default_deck_config(|config| {
        config.rwkv_review_enabled = false;
        config.fsrs_params_7 = params.clone();
    });
    let apkg_path = src_tempdir.path().join("studied.apkg");
    add_studied_card(&mut src_col, None);
    export_with_scheduling(&mut src_col, &apkg_path);

    let card = import_with_scheduling(&mut target_col, &apkg_path);

    let stored = card.memory_state.expect("a memory state");
    let expected: FsrsMemoryState = target_col
        .compute_memory_state(card.id)
        .unwrap()
        .state
        .unwrap()
        .into();
    assert!(
        (stored.stability_internal - expected.stability_internal).abs() < 0.01,
        "{stored:?} vs {expected:?}"
    );
    assert!((stored.difficulty - expected.difficulty).abs() < 0.01);
}

// Pins spec/scheduling.md#sched.apkg-import-reads-the-package: an imported
// card with Clanki's own memory state (an RWKV-Curve S90 and FSRS-7 traces)
// keeps it as the package holds it.
#[test]
fn an_imported_rwkv_curve_card_keeps_its_s90() {
    let (mut src_col, src_tempdir) = open_fs_test_collection("src");
    let (mut target_col, _target_tempdir) = open_fs_test_collection("target");
    target_col
        .set_config_bool(BoolKey::Fsrs, true, false)
        .unwrap();
    target_col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
    let apkg_path = src_tempdir.path().join("curve.apkg");
    let state = FsrsMemoryState {
        stability: 33.0,
        stability_internal: 12.0,
        stability_fast: Some(3.0),
        difficulty: 5.0,
    };
    add_studied_card(&mut src_col, Some(state));
    export_with_scheduling(&mut src_col, &apkg_path);

    let card = import_with_scheduling(&mut target_col, &apkg_path);

    assert_eq!(card.memory_state, Some(state));
}

#[test]
fn fsrs_params_stripped_on_export_without_scheduling() {
    let conf = export_and_reimport_with_fsrs_params(false);
    assert!(conf.inner.fsrs_params_4.is_empty());
    assert!(conf.inner.fsrs_params_5.is_empty());
    assert!(conf.inner.fsrs_params_6.is_empty());
}
