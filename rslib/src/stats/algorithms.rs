// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Interface A of the algorithm contract: an algorithm that predicts the
//! probability of recall of a review (spec ui.stats-model-metrics).
//!
//! Every fact here is one the ALGORITHM declares, not one the graphs know.
//! Three of 2026-09-16's defects were the same disease - a fact about one
//! algorithm written into shared code: the intersection that let 36 rows
//! silence 9154, the role list whose "first role with any row" hid every
//! per-answer row behind a backfill, and a hardcoded "this version cannot
//! compute its prediction for a past review" that was simply untrue.
//!
//! Interface B, a retention scheduler, is NOT here. RWKV-Instant does not
//! implement it: it scores a study queue and has no desired retention and
//! no due dates (spec sched.rwkv-instant-no-due-dates). Nothing in this
//! module may assume a desired retention.

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;

use crate::storage::FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE;
use crate::storage::REVIEW_PREDICTIONS_TABLE;
use crate::storage::RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE;

/// How an algorithm picks ONE sample role out of the roles it has rows for.
/// Never a shared policy: the two rules below exist for different reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RoleChoice {
    /// The first role of `honest_roles` that has any row, because that list
    /// is in order of honesty.
    FirstHonest,
    /// The role with the most rows, because every role is equally honest
    /// and the comparison should rest on as many ratings as it can.
    ///
    /// This is only correct when the STORED VALUE IS THE RAW MODEL OUTPUT
    /// and the weights are frozen. Both RWKV series qualify: RWKV-Instant
    /// stores the rating head at the query row, and RWKV-Curve stores the
    /// previous review's curve at the elapsed time, both raw. If a future
    /// series stores a value that passed through anything fitted on this
    /// collection - a calibration map, a per-collection offset - that
    /// series must use `FirstHonest` instead, because its roles then
    /// differ in honesty and the biggest one is not the safest one.
    MostRows,
}

/// Whether the algorithm can give its prediction of a review that has
/// already been answered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PastReview {
    /// It can, and these rows are how.
    Recorded,
    /// It could, but nothing has written the rows yet. The graphs say so
    /// and say what would write them; they never say the model cannot.
    NotRecordedYet,
}

/// Where an algorithm's rows live. The generic table is addressed BY
/// ALGORITHM, so a query cannot read another algorithm's number without
/// naming it. The legacy tables are one algorithm each and are still to
/// move; that move must be bit-identical.
#[derive(Debug, Clone, Copy)]
pub(crate) enum PredictionStore {
    Generic,
    Legacy(&'static str),
}

/// One algorithm, as the model-quality graphs need to know it.
#[derive(Debug, Clone, Copy)]
pub(crate) struct PredictsRecall {
    pub algorithm: SchedulingAlgorithmProto,
    /// The roles whose rows nothing fitted on the review produced. A row of
    /// any other role is never used, not even labelled.
    pub honest_roles: &'static [&'static str],
    pub role_choice: RoleChoice,
    pub past_review: PastReview,
    pub store: PredictionStore,
}

impl PredictsRecall {
    pub(crate) fn id(&self) -> i32 {
        self.algorithm as i32
    }

    pub(crate) fn table(&self) -> &'static str {
        match self.store {
            PredictionStore::Generic => REVIEW_PREDICTIONS_TABLE,
            PredictionStore::Legacy(table) => table,
        }
    }
}

/// FSRS-7's parameters are fitted on this collection, so only the rows of a
/// model that had not seen the review count: a validation fold, or a run
/// after the optimization that produced the parameters. `final_fit` rows
/// are never used. The list is in order of honesty, which is why FSRS-7
/// takes the first role that has any row.
pub(crate) const FSRS7: PredictsRecall = PredictsRecall {
    algorithm: SchedulingAlgorithmProto::Fsrs7,
    honest_roles: &["validation_fold", "post_optimization"],
    role_choice: RoleChoice::FirstHonest,
    past_review: PastReview::Recorded,
    store: PredictionStore::Legacy(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE),
};

/// RWKV's weights are frozen and were trained on other people's reviews, so
/// a replayed prediction cannot have seen the review whatever role its row
/// carries. With no honesty order to keep, it takes the role that covers
/// the most reviews.
pub(crate) const RWKV_CURVE: PredictsRecall = PredictsRecall {
    algorithm: SchedulingAlgorithmProto::RwkvCurve,
    honest_roles: &["test_fold", "post_optimization", "final_fit"],
    role_choice: RoleChoice::MostRows,
    past_review: PastReview::Recorded,
    store: PredictionStore::Generic,
};

pub(crate) const RWKV_INSTANT: PredictsRecall = PredictsRecall {
    algorithm: SchedulingAlgorithmProto::RwkvInstant,
    honest_roles: &["test_fold", "post_optimization", "final_fit"],
    role_choice: RoleChoice::MostRows,
    past_review: PastReview::Recorded,
    store: PredictionStore::Legacy(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE),
};

/// Menu order, and the order of the series in the progress message.
pub(crate) const ALGORITHMS: [PredictsRecall; 3] = [FSRS7, RWKV_CURVE, RWKV_INSTANT];
