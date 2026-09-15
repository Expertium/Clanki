// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config, DeckConfig_Config_FsrsVersion } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { fsrsParams, withFsrs7Params } from "./lib";

// Pins spec/scheduling.md#sched.fsrs7-only (the deck-options side)

test("fsrsParams returns the preset's 34 FSRS-7 parameters", () => {
    const config = new DeckConfig_Config();
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => i + 1);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config)).toStrictEqual(config.fsrsParams7);
});

test("fsrsParams ignores the stored version and older parameter sets", () => {
    const config = new DeckConfig_Config();
    config.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    config.fsrsParams4 = Array.from({ length: 17 }, (_, i) => i + 1);
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 10 + i);
    // no usable FSRS-7 parameters: [] (the backend runs the FSRS-7 defaults)
    expect(fsrsParams(config)).toStrictEqual([]);
    config.fsrsParams7 = [0.1, 0.2, 0.3];
    expect(fsrsParams(config)).toStrictEqual([]);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);
    expect(fsrsParams(config)).toStrictEqual(config.fsrsParams7);
});

test("fsrsParams rejects non-finite FSRS-7 parameters", () => {
    const config = new DeckConfig_Config();
    config.fsrsParams7 = Array.from({ length: 34 }, () => 1);
    config.fsrsParams7[3] = Number.NaN;
    expect(fsrsParams(config)).toStrictEqual([]);
});

test("withFsrs7Params updates FSRS-7 params immutably and leaves older sets", () => {
    const config = new DeckConfig_Config();
    config.fsrsVersion = DeckConfig_Config_FsrsVersion.SIX;
    config.fsrsParams6 = Array.from({ length: 21 }, (_, i) => 10 + i);
    config.fsrsParams7 = Array.from({ length: 34 }, (_, i) => 100 + i);

    const updatedParams = Array.from({ length: 34 }, (_, i) => 200 + i);
    const updated = withFsrs7Params(config, updatedParams);

    expect(updated).not.toBe(config);
    expect(updated.fsrsParams7).toStrictEqual(updatedParams);
    expect(updated.fsrsParams6).toStrictEqual(config.fsrsParams6);
    expect(updated.fsrsVersion).toBe(DeckConfig_Config_FsrsVersion.SIX);
    expect(config.fsrsParams7).not.toStrictEqual(updatedParams);
});
