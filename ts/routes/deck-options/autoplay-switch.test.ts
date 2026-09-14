// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { applyPlayAudio, playAudioFromConfig } from "./autoplay-switch";

// Pins spec/deck-options.md#deck-options.play-audio-switch
test("the switch is on while autoplay is not disabled", () => {
    expect(playAudioFromConfig({ disableAutoplay: false })).toBe(true);
    expect(playAudioFromConfig({ disableAutoplay: true })).toBe(false);
});

test("toggling writes the opposite stored value", () => {
    expect(applyPlayAudio({ disableAutoplay: false }, false)).toEqual({
        disableAutoplay: true,
    });
    expect(applyPlayAudio({ disableAutoplay: true }, true)).toEqual({
        disableAutoplay: false,
    });
    const unchanged = { disableAutoplay: true };
    expect(applyPlayAudio(unchanged, false)).toBe(unchanged);
    expect(unchanged.disableAutoplay).toBe(true);
});
