// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::stats::graphs_response::Intervals;

use crate::card::CardType;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::stats::graphs::GraphsContext;

impl GraphsContext {
    pub(super) fn intervals(&self) -> Intervals {
        let mut data = Intervals::default();
        for card in &self.cards {
            if matches!(card.ctype, CardType::Review | CardType::Relearn) {
                *data
                    .intervals
                    .entry(card.interval)
                    .or_insert_with(Default::default) += 1;
            }
        }
        data
    }

    /// Each card's S90 under the collection's algorithm: FSRS-7's, or under
    /// RWKV-Curve the S90 of the card's stored curve, never the FSRS-7 S90
    /// the card also carries (spec ui.rwkv-curve-stored-s90).
    pub(super) fn stability(&self) -> Intervals {
        let mut data = Intervals::default();
        let curve_s90s = self.rwkv_curve_s90s.as_deref();
        for card in &self.cards {
            let s90 = if self.algorithm == SchedulingAlgorithm::RwkvCurve {
                curve_s90s.and_then(|s90s| s90s.get(&card.id)).copied()
            } else {
                card.memory_state.map(|state| state.stability)
            };
            if let Some(s90) = s90 {
                *data
                    .intervals
                    .entry(s90.round() as u32)
                    .or_insert_with(Default::default) += 1;
            }
        }
        data
    }
}
