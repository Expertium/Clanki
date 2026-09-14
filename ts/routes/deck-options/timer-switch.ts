// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The Simple-mode "On-screen timer" switch stands for "Show on-screen timer"
 * and "Stop on-screen timer on answer" together (spec/deck-options.md,
 * `deck-options.simple-view`). Advanced mode keeps the two settings.
 */
export interface TimerSettings {
    showTimer: boolean;
    stopTimerOnAnswer: boolean;
}

/** The switch reads as on while the timer is shown, whatever the stop setting. */
export function onScreenTimerFromConfig(config: TimerSettings): boolean {
    return config.showTimer;
}

/**
 * Sets both settings to `on`, in place, and returns the config. A no-op when
 * the switch already reads as `on`, so showing a preset writes nothing: a
 * preset that shows the timer without stopping it keeps that until the switch
 * is toggled.
 */
export function applyOnScreenTimer<T extends TimerSettings>(config: T, on: boolean): T {
    if (onScreenTimerFromConfig(config) === on) {
        return config;
    }
    config.showTimer = on;
    config.stopTimerOnAnswer = on;
    return config;
}
