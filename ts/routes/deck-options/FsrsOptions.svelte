<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        ComputeRetentionProgress,
        type ComputeParamsProgress,
    } from "@generated/anki/collection_pb";
    import {
        evaluateParams,
        evaluateParamsLegacy,
        getFsrsNewCardIntervals,
        getRetentionWorkload,
        setWantsAbort,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { runWithBackendProgress } from "@tslib/progress";

    import SettingTitle from "$lib/components/SettingTitle.svelte";

    import {
        commitEditing,
        type DeckOptionsState,
        ValueTab,
        withFsrs7Params,
    } from "./lib";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import Warning from "./Warning.svelte";
    import ParamsInputRow from "./ParamsInputRow.svelte";
    import ParamsSearchRow from "./ParamsSearchRow.svelte";
    import SimulatorModal from "./SimulatorModal.svelte";
    import {
        fsrsParamDiagnostics,
        OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING,
        type FsrsParamDiagnostics,
    } from "./fsrs-param-diagnostics";
    import {
        DeckConfig_Config,
        DeckConfig_Config_FsrsVersion,
        GetRetentionWorkloadRequest,
        type GetRetentionWorkloadResponse,
        UpdateDeckConfigsMode,
    } from "@generated/anki/deck_config_pb";
    import type Modal from "bootstrap/js/dist/modal";
    import TabbedValue from "./TabbedValue.svelte";
    import Item from "$lib/components/Item.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import { buildSimulateFsrsRequest } from "./simulate-fsrs-request";

    export let state: DeckOptionsState;
    export let openHelpModal: (String) => void;

    export function onPresetChange() {
        desiredRetentionTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.desiredRetention,
            (value) => ($config.desiredRetention = value!),
            $config.desiredRetention,
            null,
        );
        effectiveDesiredRetention =
            $limits.desiredRetention ?? $config.desiredRetention;
    }

    const config = state.currentConfig;
    const defaults = state.defaults;
    const fsrsShortTermWithStepsEnabled = state.fsrsShortTermWithStepsEnabled;
    const reviewFuzzEnabled = state.reviewFuzzEnabled;
    const reviewFuzzBase = state.reviewFuzzBase;
    const reviewFuzzFactorShort = state.reviewFuzzFactorShort;
    const reviewFuzzFactorMid = state.reviewFuzzFactorMid;
    const reviewFuzzFactorLong = state.reviewFuzzFactorLong;
    const daysSinceLastOptimization = state.daysSinceLastOptimization;
    const limits = state.deckLimits;
    const advanced = state.advancedUi;

    // Which value the Algorithm dropdown holds for this preset (spec
    // deck-options.scheduler-choice). The interval preview and the interval
    // warnings only describe FSRS. "Optimize All Presets" shows in both
    // modes; the FSRS advanced section (parameters, version selector,
    // search filter, Check Health, simulator) is Advanced-only (spec
    // deck-options.simple-view). All of them are hidden under either RWKV
    // mode (spec deck-options.fsrs-only-controls). The collection-wide
    // settings live in Preferences (spec
    // deck-options.collection-wide-in-preferences).
    $: rwkvCurve = $config.rwkvReviewEnabled;
    $: rwkvInstant = $config.rwkvReviewInstantOrderEnabled && !rwkvCurve;
    $: rwkvMode = rwkvCurve || rwkvInstant;

    $: lastOptimizationWarning =
        $daysSinceLastOptimization > 30 ? tr.deckConfigTimeToOptimize() : "";
    const initialParams = [...$config.fsrsParams7];

    let computeParamsProgress: ComputeParamsProgress | undefined;
    let checkingParams = false;
    let checkingHealth = false;
    type FsrsParamRole = "current" | "optimized";

    class FsrsOptimizationFeedbackError extends Error {}
    function errorMessage(err: unknown): string {
        return err instanceof Error ? err.message : String(err);
    }

    function isInterrupted(err: unknown): boolean {
        return errorMessage(err) === "500: Interrupted";
    }

    function isInvalidFsrsParametersError(err: unknown): boolean {
        return errorMessage(err).includes(tr.deckConfigInvalidParameters());
    }

    function fsrsParamDiagnosticDetails(diagnostics: FsrsParamDiagnostics): string {
        if (diagnostics.outdatedFsrs7PreviewParams) {
            return OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING;
        }
        if (!diagnostics.validCount) {
            return `Expected 0 or 34 values (FSRS-7), but found ${diagnostics.count}.`;
        }
        if (diagnostics.nonFiniteIndexes.length) {
            const positions = diagnostics.nonFiniteIndexes
                .slice(0, 5)
                .map((index) => index + 1)
                .join(", ");
            return `Non-finite value at position ${positions}.`;
        }
        return "";
    }

    function invalidFsrsParamsFeedback(
        role: FsrsParamRole,
        diagnostics: FsrsParamDiagnostics,
        rejectedByBackend = false,
    ): string {
        const details = fsrsParamDiagnosticDetails(diagnostics);
        const backendDetail = rejectedByBackend
            ? "FSRS rejected these parameters during evaluation."
            : "";
        const reason = [details, backendDetail].filter((text) => text).join("\n");
        const roleMessage =
            role === "current"
                ? "Anki cannot evaluate the current FSRS parameters, so it cannot compare them with the optimized parameters."
                : "Anki cannot evaluate the optimized FSRS parameters, so it cannot show the optimization comparison.";
        let nextStep = "Please report this with the console details.";
        if (role === "current") {
            nextStep = diagnostics.outdatedFsrs7PreviewParams
                ? ""
                : "Leave the FSRS parameters field blank to use the default values, then optimize again.";
        }

        return [
            roleMessage,
            reason,
            nextStep,
            "Details have been logged to the console.",
        ]
            .filter((text) => text)
            .join("\n\n");
    }

    function logFsrsParamProblem(
        reason: string,
        role: FsrsParamRole,
        params: number[],
        err?: unknown,
    ): void {
        const details = {
            params,
            diagnostics: fsrsParamDiagnostics(params),
            enableSchedulingPenalties: enableSchedulingPenaltiesOverride(),
            search: optimizeSearchFilter(),
            evaluationSearch: evaluateSearchFilter(),
            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs().toString(),
            error: err ? errorMessage(err) : undefined,
        };
        console.warn(
            `FSRS ${role} parameter ${reason}: ${JSON.stringify(details)}`,
            err,
        );
    }

    function requireValidFsrsParams(role: FsrsParamRole, params: number[]): void {
        const diagnostics = fsrsParamDiagnostics(params);
        if (diagnostics.valid) {
            return;
        }
        logFsrsParamProblem("validation failed before backend call", role, params);
        throw new FsrsOptimizationFeedbackError(
            invalidFsrsParamsFeedback(role, diagnostics),
        );
    }

    function optimizationFailureFeedback(err: unknown, params: number[]): string {
        if (err instanceof FsrsOptimizationFeedbackError) {
            return err.message;
        }

        const diagnostics = fsrsParamDiagnostics(params);
        if (isInvalidFsrsParametersError(err) && !diagnostics.valid) {
            logFsrsParamProblem(
                "backend rejected current parameters",
                "current",
                params,
                err,
            );
            return invalidFsrsParamsFeedback("current", diagnostics, true);
        }

        console.warn(
            "FSRS optimization failed",
            {
                params,
                diagnostics,
                search: optimizeSearchFilter(),
                evaluationSearch: evaluateSearchFilter(),
                ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs().toString(),
                error: errorMessage(err),
            },
            err,
        );

        return `FSRS optimization failed. Details have been logged to the console.\n\n${errorMessage(err)}`;
    }

    $: computing = checkingParams || checkingHealth;
    $: defaultparamSearch = `preset:"${state.getCurrentNameForSearch()}" -is:suspended`;
    $: roundedRetention = Number(effectiveDesiredRetention.toFixed(2));
    $: desiredRetentionWarning = getRetentionLongShortWarning(roundedRetention);

    let desiredRetentionChangeInfo = "";
    let desiredRetentionChangeClass = "alert-info two-line";
    // FSRS-7's workload estimate; RWKV has none, and FSRS-7's never stands in
    // (spec deck-options.desired-retention-note)
    $: if (!rwkvMode) {
        getRetentionChangeInfo(roundedRetention, $config.fsrsParams7);
    }

    $: retentionWarningClass = getRetentionWarningClass(roundedRetention);

    $: newCardsIgnoreReviewLimit = state.newCardsIgnoreReviewLimit;

    // Create tabs for desired retention
    const desiredRetentionTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.desiredRetention,
            (value) => ($config.desiredRetention = value!),
            $config.desiredRetention,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.desiredRetention ?? null,
            (value) => ($limits.desiredRetention = value ?? undefined),
            null,
            null,
        ),
    ];

    // Get the effective desired retention value (deck-specific if set, otherwise config default)
    let effectiveDesiredRetention =
        $limits.desiredRetention ?? $config.desiredRetention;
    const startingDesiredRetention = effectiveDesiredRetention.toFixed(2);
    const startingDesiredRetentionValue = Number(startingDesiredRetention);
    const intervalColumns = [
        tr.studyingAgain(),
        tr.studyingHard(),
        tr.studyingGood(),
        tr.studyingEasy(),
        tr.deckConfigAgainThenGood(),
        tr.deckConfigAgainThenAgain(),
        tr.deckConfigGoodThenAgain(),
        tr.deckConfigGoodThenGood(),
    ];
    // Only the first answer's intervals are shown (spec
    // deck-options.first-intervals); the follow-up rows stay in the RPC.
    const firstIntervalColumns = intervalColumns.slice(0, 4);
    const intervalRowClasses = [
        "interval-again",
        "interval-hard",
        "interval-good",
        "interval-easy",
        "",
        "",
        "",
        "",
    ];
    let newCardIntervals: [string[], string[]] | undefined;
    let newCardIntervalsError = "";
    let newCardIntervalRequest = 0;

    $: simulateFsrsRequest = buildSimulateFsrsRequest({
        config: $config,
        params: $config.fsrsParams7,
        search: `preset:"${state.getCurrentNameForSearch()}" -is:suspended`,
        newCardsIgnoreReviewLimit: $newCardsIgnoreReviewLimit,
        reviewFuzzEnabled: $reviewFuzzEnabled,
        reviewFuzzBase: $reviewFuzzBase,
        reviewFuzzFactorShort: $reviewFuzzFactorShort,
        reviewFuzzFactorMid: $reviewFuzzFactorMid,
        reviewFuzzFactorLong: $reviewFuzzFactorLong,
    });

    $: void loadNewCardIntervals(
        startingDesiredRetentionValue,
        effectiveDesiredRetention,
        $fsrsShortTermWithStepsEnabled,
        $config.maxSameDayReviews,
        $config.fsrsParams7,
        $config.learnSteps,
        $config.relearnSteps,
        $config.maximumReviewInterval,
        $config.fsrsMinimumIntervalSecs,
        $config.graduatingIntervalGood,
        $config.graduatingIntervalEasy,
        $config.initialEase,
        $config.hardMultiplier,
        $config.easyMultiplier,
        $config.intervalMultiplier,
        $config.leechThreshold,
        $config.lapseMultiplier,
        $config.minimumLapseInterval,
    );

    const DESIRED_RETENTION_LOW_THRESHOLD = 0.8;
    const DESIRED_RETENTION_HIGH_THRESHOLD = 0.95;

    function getRetentionLongShortWarning(retention: number) {
        if (retention < DESIRED_RETENTION_LOW_THRESHOLD) {
            return tr.deckConfigDesiredRetentionTooLow();
        } else if (retention > DESIRED_RETENTION_HIGH_THRESHOLD) {
            return tr.deckConfigDesiredRetentionTooHigh();
        } else {
            return "";
        }
    }

    let retentionWorkloadInfo: undefined | Promise<GetRetentionWorkloadResponse> =
        undefined;
    let lastParams = [...$config.fsrsParams7];

    function configWithDesiredRetention(
        currentConfig: DeckConfig_Config,
        desiredRetention: number,
    ): DeckConfig_Config {
        const config = new DeckConfig_Config(currentConfig);
        config.desiredRetention = desiredRetention;
        return config;
    }

    async function loadNewCardIntervals(
        currentRetention: number,
        selectedRetention: number,
        fsrsShortTermWithStepsEnabled: boolean,
        _maxSameDayReviews: number | undefined,
        params: number[],
        _learnSteps: number[],
        _relearnSteps: number[],
        _maximumReviewInterval: number,
        _fsrsMinimumIntervalSecs: number,
        _graduatingIntervalGood: number,
        _graduatingIntervalEasy: number,
        _initialEase: number,
        _hardMultiplier: number,
        _easyMultiplier: number,
        _intervalMultiplier: number,
        _leechThreshold: number,
        _lapseMultiplier: number,
        _minimumLapseInterval: number,
    ): Promise<void> {
        const request = ++newCardIntervalRequest;
        newCardIntervalsError = "";
        const diagnostics = fsrsParamDiagnostics(params);
        if (!diagnostics.valid) {
            newCardIntervals = undefined;
            newCardIntervalsError = fsrsParamDiagnosticDetails(diagnostics);
            return;
        }
        const currentConfig = withFsrs7Params($config, params);
        try {
            const [current, selected] = await Promise.all([
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(currentConfig, currentRetention),
                    fsrsShortTermWithStepsEnabled,
                }),
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(
                        currentConfig,
                        selectedRetention,
                    ),
                    fsrsShortTermWithStepsEnabled,
                }),
            ]);
            if (request !== newCardIntervalRequest) {
                return;
            }
            newCardIntervals = [current.vals, selected.vals];
            newCardIntervalsError = "";
        } catch (err) {
            if (request === newCardIntervalRequest) {
                newCardIntervals = undefined;
                newCardIntervalsError =
                    err instanceof Error ? err.message : String(err);
                console.error("failed to load FSRS new-card intervals", err);
            }
        }
    }

    async function getRetentionChangeInfo(retention: number, params: number[]) {
        if (+startingDesiredRetention == roundedRetention) {
            desiredRetentionChangeInfo = tr.deckConfigWorkloadFactorUnchanged();
            desiredRetentionChangeClass = "alert-info two-line";
            return;
        }
        const diagnostics = fsrsParamDiagnostics(params);
        if (!diagnostics.valid) {
            lastParams = [...params];
            retentionWorkloadInfo = undefined;
            desiredRetentionChangeInfo = fsrsParamDiagnosticDetails(diagnostics);
            desiredRetentionChangeClass = "alert-warning two-line";
            return;
        }
        if (
            // If the cache is empty and a request has not yet been made to fill it
            !retentionWorkloadInfo ||
            // If the parameters have been changed
            lastParams.toString() !== params.toString()
        ) {
            const request = new GetRetentionWorkloadRequest({
                w: params,
                search: defaultparamSearch,
            });
            lastParams = [...params];
            retentionWorkloadInfo = getRetentionWorkload(request, {
                alertOnError: false,
            });
        }

        const previous = +startingDesiredRetention * 100;
        const after = retention * 100;
        try {
            const resp = await retentionWorkloadInfo;
            const factor = resp.costs[after] / resp.costs[previous];

            desiredRetentionChangeInfo = tr.deckConfigWorkloadFactorChange({
                factor: factor.toFixed(2),
                previousDr: previous.toString(),
            });
            desiredRetentionChangeClass = "alert-info two-line";
        } catch (err) {
            retentionWorkloadInfo = undefined;
            desiredRetentionChangeInfo = errorMessage(err);
            desiredRetentionChangeClass = "alert-warning two-line";
            console.warn("failed to load FSRS retention workload", err);
        }
    }

    function getRetentionWarningClass(retention: number): string {
        if (retention < 0.7 || retention > 0.97) {
            return "alert-danger";
        } else if (
            retention < DESIRED_RETENTION_LOW_THRESHOLD ||
            retention > DESIRED_RETENTION_HIGH_THRESHOLD
        ) {
            return "alert-warning";
        } else {
            return "alert-info";
        }
    }

    function getIgnoreRevlogsBeforeMs() {
        return BigInt(
            $config.ignoreRevlogsBeforeDate
                ? new Date($config.ignoreRevlogsBeforeDate).getTime()
                : 0,
        );
    }

    function getNumOfRelearningStepsInDay(): number {
        const relearningSteps = $config.relearnSteps;
        let numOfRelearningStepsInDay = 0;
        let accumulatedTime = 0;
        for (let i = 0; i < relearningSteps.length; i++) {
            accumulatedTime += relearningSteps[i];
            if (accumulatedTime >= 1440) {
                break;
            }
            numOfRelearningStepsInDay++;
        }
        return numOfRelearningStepsInDay;
    }

    function optimizeSearchFilter(): string {
        return $config.paramSearch ? $config.paramSearch : defaultparamSearch;
    }

    // Evaluation uses the same search as optimization; there is no separate
    // evaluation filter (spec deck-options.fsrs-only-controls).
    function evaluateSearchFilter(): string {
        return optimizeSearchFilter();
    }

    // FSRS-7 always trains on same-day reviews (the backend ignores the
    // request field) and always uses scheduling penalties; neither is a user
    // setting (spec deck-options.fsrs-only-controls, sched.fsrs7-only).
    function enableSchedulingPenaltiesOverride(): boolean {
        return true;
    }

    async function checkParams(): Promise<void> {
        if (checkingParams) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        const params = $config.fsrsParams7;
        checkingParams = true;
        computeParamsProgress = undefined;
        try {
            requireValidFsrsParams("current", params);
            await runWithBackendProgress(
                async () => {
                    const search = evaluateSearchFilter();
                    const resp = await evaluateParamsLegacy(
                        {
                            search,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            params,
                        },
                        { alertOnError: false },
                    );
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                    setTimeout(
                        () =>
                            alert(
                                `Log loss: ${resp.logLoss.toFixed(4)}, RMSE(bins): ${(
                                    resp.rmseBins * 100
                                ).toFixed(2)}%. ${tr.deckConfigSmallerIsBetter()}`,
                            ),
                        200,
                    );
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } catch (err) {
            if (!isInterrupted(err)) {
                alert(optimizationFailureFeedback(err, params));
            }
        } finally {
            checkingParams = false;
        }
    }

    async function checkHealth(): Promise<void> {
        if (checkingHealth) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        const params = $config.fsrsParams7;
        checkingHealth = true;
        computeParamsProgress = undefined;
        try {
            requireValidFsrsParams("current", params);
            await runWithBackendProgress(
                async () => {
                    const search = evaluateSearchFilter();
                    const searchForTraining = optimizeSearchFilter();
                    const resp = await evaluateParams(
                        {
                            search,
                            searchForTraining,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            numOfRelearningSteps: getNumOfRelearningStepsInDay(),
                            // required on the wire; the backend runs FSRS-7
                            // whatever it says (spec sched.fsrs7-only)
                            fsrsVersion: DeckConfig_Config_FsrsVersion.SEVEN,
                            enableSchedulingPenalties:
                                enableSchedulingPenaltiesOverride(),
                        },
                        { alertOnError: false },
                    );
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
                    setTimeout(
                        () =>
                            alert(
                                `Log loss: ${resp.logLoss.toFixed(4)}, RMSE(bins): ${(
                                    resp.rmseBins * 100
                                ).toFixed(2)}%. ${tr.deckConfigSmallerIsBetter()}`,
                            ),
                        200,
                    );
                },
                (progress) => {
                    if (progress.value.case === "computeParams") {
                        computeParamsProgress = progress.value.value;
                    }
                },
            );
        } catch (err) {
            if (!isInterrupted(err)) {
                alert(optimizationFailureFeedback(err, params));
            }
        } finally {
            checkingHealth = false;
        }
    }

    $: computeParamsProgressString = renderWeightProgress(computeParamsProgress);
    $: computeParamsProgressPct = renderComputeParamsProgressPct(computeParamsProgress);
    $: totalReviews = computeParamsProgress?.reviews ?? undefined;

    function renderComputeParamsProgressPct(
        val: ComputeParamsProgress | undefined,
    ): number | undefined {
        if (!val || !val.total) {
            return undefined;
        }
        return Math.min(100, Math.max(0, (val.current / val.total) * 100));
    }

    function renderWeightProgress(val: ComputeParamsProgress | undefined): String {
        const pctValue = renderComputeParamsProgressPct(val);
        if (!val || pctValue === undefined) {
            return "";
        }
        const pct = pctValue.toFixed(1);
        if (val instanceof ComputeRetentionProgress) {
            return `${pct}%`;
        } else {
            if (val.current === val.total) {
                return tr.deckConfigCheckingForImprovement();
            } else {
                return tr.deckConfigPercentOfReviews({ pct, reviews: val.reviews });
            }
        }
    }

    async function computeAllParams(): Promise<void> {
        await commitEditing();
        // A preset whose parameters the optimizer cannot use gets the
        // default parameters, with no question (Andrew, 2026-09-16).
        state.prepareComputeAllParams();
        state.save(UpdateDeckConfigsMode.COMPUTE_ALL_PARAMS);
    }

    function showSimulatorModal(modal: Modal) {
        const params = $config.fsrsParams7;
        const diagnostics = fsrsParamDiagnostics(params);
        if (!diagnostics.valid) {
            logFsrsParamProblem(
                "validation failed before simulator",
                "current",
                params,
            );
            alert(fsrsParamDiagnosticDetails(diagnostics));
            return;
        }
        if (params.toString() === initialParams.toString()) {
            modal?.show();
        } else {
            alert(tr.deckConfigFsrsSimulateSavePreset());
        }
    }

    let simulatorModal: Modal;
    let workloadModal: Modal;
    $: outdatedFsrs7ParamsWarning = fsrsParamDiagnostics($config.fsrsParams7)
        .outdatedFsrs7PreviewParams
        ? OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING
        : "";
</script>

<DynamicallySlottable slotHost={Item} api={{}}>
    <Item>
        <SpinBoxFloatRow
            bind:value={effectiveDesiredRetention}
            defaultValue={defaults.desiredRetention}
            min={0.1}
            max={0.99}
            percentage={true}
        >
            <TabbedValue
                slot="tabs"
                tabs={desiredRetentionTabs}
                bind:value={effectiveDesiredRetention}
                showTabs={$advanced}
            />
            <SettingTitle on:click={() => openHelpModal("desiredRetention")}>
                {tr.deckConfigDesiredRetention()}
            </SettingTitle>
        </SpinBoxFloatRow>
    </Item>
</DynamicallySlottable>
{#if rwkvInstant}
    <Warning
        warning={tr.deckConfigRwkvInstantRetentionInfo()}
        className="alert-info two-line"
    />
{:else}
    <!-- Shown from the moment the page opens: the plain note until the value
         changes, then FSRS-7's workload estimate; RWKV-Curve keeps the note
         (spec deck-options.desired-retention-note). -->
    <Warning
        warning={rwkvCurve
            ? tr.deckConfigWorkloadFactorUnchanged()
            : desiredRetentionChangeInfo}
        className={rwkvCurve ? "alert-info two-line" : desiredRetentionChangeClass}
    />
    <Warning warning={desiredRetentionWarning} className={retentionWarningClass} />
{/if}

{#if !rwkvMode && newCardIntervals}
    <div class="interval-preview ms-1 me-1">
        <div class="interval-preview-title">
            {tr.deckConfigFirstIntervals()}
        </div>
        <table class="interval-preview-table">
            <thead>
                <tr>
                    <th></th>
                    <th>
                        {tr.deckConfigCurrentDr()}
                        ({(startingDesiredRetentionValue * 100).toFixed(2)}%)
                    </th>
                    <th>
                        {tr.deckConfigSelectedDr()}
                        ({(effectiveDesiredRetention * 100).toFixed(2)}%)
                    </th>
                </tr>
            </thead>
            <tbody>
                {#each firstIntervalColumns as column, index}
                    <tr class={intervalRowClasses[index]}>
                        <th>{column}</th>
                        <td>{newCardIntervals[0][index]}</td>
                        <td>{newCardIntervals[1][index]}</td>
                    </tr>
                {/each}
            </tbody>
        </table>
    </div>
{/if}

{#if !rwkvMode}
    <Warning warning={newCardIntervalsError} className={"alert-warning"} />
{/if}
<Warning warning={outdatedFsrs7ParamsWarning} className="alert-warning" />

<!-- "Reschedule cards when desired retention changes" is a Preferences
     setting (spec deck-options.collection-wide-in-preferences). -->

<!-- One optimize action for every preset, shown in both modes (spec
     deck-options.fsrs-only-controls). -->
{#if !rwkvMode}
    <div class="ms-1 me-1">
        <button class="btn btn-primary" on:click={() => computeAllParams()}>
            {tr.deckConfigSaveAndOptimize()}
        </button>
    </div>
{/if}

{#if !rwkvMode && $advanced}
    <details class="fsrs-advanced m-1">
        <summary>{tr.deckConfigAdvancedSettings()}</summary>

        <div>
            <button
                class="btn btn-outline-primary"
                on:click={() => {
                    simulateFsrsRequest.reviewLimit = 9999;
                    showSimulatorModal(workloadModal);
                }}
            >
                {tr.deckConfigFsrsDesiredRetentionHelpMeDecideExperimental()}
            </button>
        </div>

        <Warning warning={lastOptimizationWarning} className="alert-warning" />

        <!-- FSRS-7 is the only model (spec sched.fsrs7-only): no version
             selector; empty parameters run the FSRS-7 defaults. -->
        <ParamsInputRow bind:value={$config.fsrsParams7} defaultValue={[]}>
            <SettingTitle on:click={() => openHelpModal("modelParams")}>
                {tr.deckConfigWeights()}
            </SettingTitle>
        </ParamsInputRow>

        <ParamsSearchRow
            bind:value={$config.paramSearch}
            placeholder={defaultparamSearch}
        >
            <SettingTitle>Search Filter</SettingTitle>
        </ParamsSearchRow>

        <button
            class="btn {checkingHealth ? 'btn-warning' : 'btn-primary'}"
            disabled={!checkingHealth && computing}
            on:click={() => checkHealth()}
        >
            {#if checkingHealth}
                {tr.actionsCancel()}
            {:else}
                {tr.deckConfigHealthCheckButton()}
            {/if}
        </button>
        {#if state.legacyEvaluate}
            <button
                class="btn {checkingParams ? 'btn-warning' : 'btn-primary'}"
                disabled={!checkingParams && computing}
                on:click={() => checkParams()}
            >
                {#if checkingParams}
                    {tr.actionsCancel()}
                {:else}
                    {tr.deckConfigEvaluateButton()}
                {/if}
            </button>
        {/if}
        <div>
            {#if checkingParams || checkingHealth}
                {computeParamsProgressString}
                {#if computeParamsProgressPct !== undefined}
                    <div
                        class="progress fsrs-progress"
                        role="progressbar"
                        aria-valuenow={computeParamsProgressPct}
                        aria-valuemin="0"
                        aria-valuemax="100"
                    >
                        <div
                            class="progress-bar"
                            style={`width: ${computeParamsProgressPct}%`}
                        ></div>
                    </div>
                {/if}
            {:else if totalReviews !== undefined}
                {tr.statisticsReviews({ reviews: totalReviews })}
            {/if}
        </div>
        <button
            class="btn btn-primary"
            on:click={() => showSimulatorModal(simulatorModal)}
        >
            {tr.deckConfigFsrsSimulatorExperimental()}
        </button>
    </details>
{/if}

<SimulatorModal
    bind:modal={simulatorModal}
    {state}
    {simulateFsrsRequest}
    {computing}
    {openHelpModal}
    {onPresetChange}
/>

<SimulatorModal
    bind:modal={workloadModal}
    workload
    {state}
    {simulateFsrsRequest}
    {computing}
    {openHelpModal}
    {onPresetChange}
/>

<style>
    .btn {
        margin-bottom: 0.375rem;
    }

    .fsrs-advanced {
        border-top: 1px solid var(--border);
        padding-top: 0.75rem;
    }

    .fsrs-advanced summary {
        cursor: pointer;
        font-weight: 700;
        margin-bottom: 0.75rem;
    }

    .interval-preview {
        margin-bottom: 0.75rem;
        overflow-x: auto;
    }

    .interval-preview-title {
        font-weight: 600;
        margin-bottom: 0.375rem;
    }

    .interval-preview-table {
        width: 100%;
        font-size: 0.9rem;
        border-collapse: collapse;
    }

    .interval-preview-table th,
    .interval-preview-table td {
        padding: 0.35rem 0.5rem;
        border: 1px solid var(--border);
        text-align: left;
        white-space: nowrap;
    }

    .interval-preview-table thead th {
        background: var(--canvas-elevated);
    }

    .interval-preview-table tr.interval-again th,
    .interval-preview-table tr.interval-again td {
        color: var(--fg-red, #b42318);
    }

    .interval-preview-table tr.interval-hard th,
    .interval-preview-table tr.interval-hard td {
        color: var(--fg-orange, #b54708);
    }

    .interval-preview-table tr.interval-good th,
    .interval-preview-table tr.interval-good td {
        color: var(--fg-green, #027a48);
    }

    .interval-preview-table tr.interval-easy th,
    .interval-preview-table tr.interval-easy td {
        color: var(--fg-light-green, #12b76a);
    }

    /* as high as the text, with less padding than a plain alert */
    :global(.two-line) {
        white-space: pre-wrap;
        padding-block: 0.5rem;
    }

    .fsrs-progress {
        width: min(24rem, 100%);
        height: 0.5rem;
        margin-top: 0.35rem;
    }
</style>
