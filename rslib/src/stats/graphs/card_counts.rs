// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::stats::graphs_response::card_counts::Counts;
use anki_proto::stats::graphs_response::CardCounts;

use crate::card::CardQueue;
use crate::card::CardType;
use crate::stats::graphs::GraphsContext;

/// Cards alike for the Card Counts graph: type, queue, whether the interval
/// is at least 21 days, and how many cards.
pub(crate) type CardCountGroup = (CardType, CardQueue, bool, u32);

impl GraphsContext {
    pub(super) fn card_counts(&self) -> CardCounts {
        card_counts(
            self.cards
                .iter()
                .map(|card| (card.ctype, card.queue, card.interval >= 21, 1)),
        )
    }
}

pub(super) fn card_counts(groups: impl Iterator<Item = CardCountGroup>) -> CardCounts {
    let mut excluding_inactive = Counts::default();
    let mut including_inactive = Counts::default();
    for (ctype, queue, mature, cards) in groups {
        match queue {
            CardQueue::Suspended => {
                excluding_inactive.suspended += cards;
            }
            CardQueue::SchedBuried | CardQueue::UserBuried => {
                excluding_inactive.buried += cards;
            }
            _ => increment_counts(&mut excluding_inactive, ctype, mature, cards),
        };
        increment_counts(&mut including_inactive, ctype, mature, cards);
    }
    CardCounts {
        excluding_inactive: Some(excluding_inactive),
        including_inactive: Some(including_inactive),
    }
}

fn increment_counts(counts: &mut Counts, ctype: CardType, mature: bool, cards: u32) {
    match ctype {
        CardType::New => {
            counts.new_cards += cards;
        }
        CardType::Learn => {
            counts.learn += cards;
        }
        CardType::Review => {
            if mature {
                counts.mature += cards;
            } else {
                counts.young += cards;
            }
        }
        CardType::Relearn => {
            counts.relearn += cards;
        }
    }
}
