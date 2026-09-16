// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * What the model-quality graphs' menus have chosen (spec
 * ui.stats-model-metrics). The choice lives beside the graphs rather than
 * inside them, so it survives the page reloading its data, a Simple /
 * Advanced switch and a change of period, and choosing again never restarts
 * a finished computation: the data is the same for every algorithm.
 */

import type { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import { writable } from "svelte/store";

/** The calibration graph's one algorithm; null = the first that has data. */
export const chosenCalibrationAlgorithm = writable<SchedulingAlgorithm | null>(null);

/** The UM+ graph's pair, as "<first>-<second>"; null = the first pair. */
export const chosenUmPlusPair = writable<string | null>(null);

/** The UM+ graph's switch for groups of fewer than 200 ratings. */
export const showSmallUmPlusGroups = writable(false);
