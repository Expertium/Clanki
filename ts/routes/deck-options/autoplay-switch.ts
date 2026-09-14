// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The "Play audio automatically" switch is the stored `disableAutoplay`
 * setting turned the other way round (spec/deck-options.md,
 * `deck-options.play-audio-switch`): on means audio plays.
 */
export interface AutoplaySettings {
    disableAutoplay: boolean;
}

export function playAudioFromConfig(config: AutoplaySettings): boolean {
    return !config.disableAutoplay;
}

/** Sets the stored setting so the switch reads as `on`, in place; a no-op
 * when it already does, so showing a preset writes nothing. */
export function applyPlayAudio<T extends AutoplaySettings>(config: T, on: boolean): T {
    if (playAudioFromConfig(config) !== on) {
        config.disableAutoplay = !on;
    }
    return config;
}
