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
        computeFsrsParams,
        evaluateParams,
        evaluateParamsLegacy,
        getFsrsNewCardIntervals,
        getRetentionWorkload,
        setWantsAbort,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { runWithBackendProgress } from "@tslib/progress";

    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";

    import GlobalLabel from "./GlobalLabel.svelte";
    import {
        commitEditing,
        type DeckOptionsState,
        ValueTab,
        withSelectedFsrsParams,
    } from "./lib";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import Warning from "./Warning.svelte";
    import ParamsInputRow from "./ParamsInputRow.svelte";
    import ParamsSearchRow from "./ParamsSearchRow.svelte";
    import SimulatorModal from "./SimulatorModal.svelte";
    import {
        deltaClass,
        formatDelta,
        formatMetric,
        formatPercentDelta,
        metricDelta,
        metricDeltaPercent,
    } from "./optimize-comparison";
    import {
        customDecayCandidates,
        formatDecay,
        supportsCustomDecayTable,
        withLastParam,
    } from "./custom-decay-table";
    import {
        fsrsParamDiagnostics,
        fsrsParamsSupportSameDayEvaluation,
        fsrsSameDayEvaluationOverrideForComparison,
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
    export let newlyEnabled = false;

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
    const fsrsReschedule = state.fsrsReschedule;
    const fsrsShortTermWithStepsEnabled = state.fsrsShortTermWithStepsEnabled;
    const fsrsLearningQueuesDisabled = state.fsrsLearningQueuesDisabled;
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
    // warnings only describe FSRS. The reschedule switch, the optimize
    // buttons and the FSRS advanced section (parameters, version selector,
    // search filter, health check, simulator) are Advanced-only (spec
    // deck-options.simple-view) and hidden under either RWKV mode (spec
    // deck-options.fsrs-only-controls).
    $: rwkvCurve = $config.rwkvReviewEnabled;
    $: rwkvInstant = $config.rwkvReviewInstantOrderEnabled && !rwkvCurve;
    $: rwkvMode = rwkvCurve || rwkvInstant;

    $: lastOptimizationWarning =
        $daysSinceLastOptimization > 30 ? tr.deckConfigTimeToOptimize() : "";
    let desiredRetentionFocused = false;
    let desiredRetentionEverFocused = false;
    let optimized = false;
    const initialParams = [...selectedFsrsParams($config)];
    $: if (desiredRetentionFocused) {
        desiredRetentionEverFocused = true;
    }
    $: showDesiredRetentionTooltip =
        newlyEnabled || desiredRetentionEverFocused || optimized;

    let computeParamsProgress: ComputeParamsProgress | undefined;
    let computingParams = false;
    let checkingParams = false;
    let checkingHealth = false;
    type OptimizationMetrics = {
        logLoss: number;
        rmseBins: number;
    };
    type OptimizationComparison = {
        optimizedParams: number[];
        search: string;
        ignoreRevlogsBeforeMs: bigint;
        includeSameDayReviews: boolean | undefined;
        current: OptimizationMetrics;
        optimized: OptimizationMetrics;
    };
    type DecayRow = {
        decay: number;
        isBaseline: boolean;
        logLoss: number;
        rmseBins: number;
        logLossDelta: number;
        logLossDeltaPercent: number | undefined;
        rmseDelta: number;
        rmseDeltaPercent: number | undefined;
    };
    type FsrsParamRole = "current" | "optimized";

    class FsrsOptimizationFeedbackError extends Error {}
    let optimizationComparison: OptimizationComparison | undefined;
    let customDecayRows: DecayRow[] = [];
    let loadingCustomDecayTable = false;
    const fsrsVersionChoices = [
        {
            value: DeckConfig_Config_FsrsVersion.SEVEN,
            label: "FSRS-7",
        },
        {
            value: DeckConfig_Config_FsrsVersion.SIX,
            label: "FSRS-6",
        },
        {
            value: DeckConfig_Config_FsrsVersion.FIVE,
            label: "FSRS-5",
        },
        {
            value: DeckConfig_Config_FsrsVersion.FOUR,
            label: "FSRS-4.5",
        },
    ];

    function selectedFsrsParams(config: DeckConfig_Config): number[] {
        switch (config.fsrsVersion) {
            case DeckConfig_Config_FsrsVersion.SIX:
                return config.fsrsParams6;
            case DeckConfig_Config_FsrsVersion.FIVE:
                return config.fsrsParams5;
            case DeckConfig_Config_FsrsVersion.FOUR:
                return config.fsrsParams4;
            default:
                return config.fsrsParams7;
        }
    }

    function setSelectedFsrsParams(params: number[]): void {
        config.update((current) => withSelectedFsrsParams(current, params));
    }

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
            return `Expected 0, 17, 19, 21, or 34 values, but found ${diagnostics.count}.`;
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
            fsrsVersion: $config.fsrsVersion,
            includeSameDayReviews: includeSameDayOverride(),
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

    async function evaluateParamsLegacyForOptimization(
        role: FsrsParamRole,
        input: Parameters<typeof evaluateParamsLegacy>[0],
    ): ReturnType<typeof evaluateParamsLegacy> {
        requireValidFsrsParams(role, input.params);
        try {
            return await evaluateParamsLegacy(input, { alertOnError: false });
        } catch (err) {
            logFsrsParamProblem("backend evaluation failed", role, input.params, err);
            if (isInvalidFsrsParametersError(err)) {
                throw new FsrsOptimizationFeedbackError(
                    invalidFsrsParamsFeedback(
                        role,
                        fsrsParamDiagnostics(input.params),
                        true,
                    ),
                );
            }
            throw err;
        }
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
                fsrsVersion: $config.fsrsVersion,
                includeSameDayReviews: includeSameDayOverride(),
                search: optimizeSearchFilter(),
                evaluationSearch: evaluateSearchFilter(),
                ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs().toString(),
                error: errorMessage(err),
            },
            err,
        );

        return `FSRS optimization failed. Details have been logged to the console.\n\n${errorMessage(err)}`;
    }

    const healthCheck = state.fsrsHealthCheck;

    $: computing = computingParams || checkingParams || checkingHealth;
    $: defaultparamSearch = `preset:"${state.getCurrentNameForSearch()}" -is:suspended`;
    $: roundedRetention = Number(effectiveDesiredRetention.toFixed(2));
    $: desiredRetentionWarning = getRetentionLongShortWarning(roundedRetention);

    let desiredRetentionChangeInfo = "";
    let desiredRetentionChangeClass = "alert-info two-line";
    $: if (showDesiredRetentionTooltip) {
        getRetentionChangeInfo(roundedRetention, selectedFsrsParams($config));
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
        params: selectedFsrsParams($config),
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
        $fsrsLearningQueuesDisabled,
        selectedFsrsParams($config),
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
    let lastParams = [...selectedFsrsParams($config)];

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
        fsrsLearningQueuesDisabled: boolean,
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
        const currentConfig = withSelectedFsrsParams($config, params);
        try {
            const [current, selected] = await Promise.all([
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(currentConfig, currentRetention),
                    fsrsShortTermWithStepsEnabled,
                    fsrsLearningQueuesDisabled,
                }),
                getFsrsNewCardIntervals({
                    config: configWithDesiredRetention(
                        currentConfig,
                        selectedRetention,
                    ),
                    fsrsShortTermWithStepsEnabled,
                    fsrsLearningQueuesDisabled,
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

    // FSRS-7 always trains on same-day reviews and never uses scheduling
    // penalties; neither is a user setting (spec
    // deck-options.fsrs-only-controls).
    function includeSameDayOverride(): boolean | undefined {
        if ($config.fsrsVersion !== DeckConfig_Config_FsrsVersion.SEVEN) {
            return undefined;
        }
        return true;
    }

    function enableSchedulingPenaltiesOverride(): boolean {
        return false;
    }

    function includeSameDayOverrideForParams(params: number[]): boolean | undefined {
        return fsrsParamsSupportSameDayEvaluation(params)
            ? includeSameDayOverride()
            : undefined;
    }

    function includeSameDayOverrideForComparison(
        currentParams: number[],
        optimizedParams: number[],
    ): boolean | undefined {
        return fsrsSameDayEvaluationOverrideForComparison(
            currentParams,
            optimizedParams,
            includeSameDayOverride(),
        );
    }

    async function computeParams(): Promise<void> {
        if (computingParams) {
            await setWantsAbort({});
            return;
        }
        if (state.presetAssignmentsChanged()) {
            alert(tr.deckConfigPleaseSaveYourChangesFirst());
            return;
        }
        await commitEditing();
        computingParams = true;
        computeParamsProgress = undefined;
        const params = selectedFsrsParams($config);
        try {
            requireValidFsrsParams("current", params);
            await runWithBackendProgress(
                async () => {
                    const search = optimizeSearchFilter();
                    const evaluateSearch = evaluateSearchFilter();
                    const resp = await computeFsrsParams(
                        {
                            search,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            currentParams: params,
                            numOfRelearningSteps: getNumOfRelearningStepsInDay(),
                            healthCheck: $healthCheck,
                            includeSameDayReviews: includeSameDayOverride(),
                            enableSchedulingPenalties:
                                enableSchedulingPenaltiesOverride(),
                            fsrsVersion: $config.fsrsVersion,
                        },
                        { alertOnError: false },
                    );
                    requireValidFsrsParams("optimized", resp.params);

                    const alreadyOptimal =
                        (params.length &&
                            params.every(
                                (n, i) => n.toFixed(4) === resp.params[i].toFixed(4),
                            )) ||
                        resp.params.length === 0;

                    let healthCheckMessage = "";
                    if (resp.healthCheckPassed !== undefined) {
                        healthCheckMessage = resp.healthCheckPassed
                            ? tr.deckConfigFsrsGoodFit()
                            : "";
                    }
                    let alreadyOptimalMessage = "";
                    if (alreadyOptimal) {
                        alreadyOptimalMessage = resp.fsrsItems
                            ? tr.deckConfigFsrsParamsOptimal()
                            : tr.deckConfigFsrsParamsNoReviews();
                    }
                    const message = [alreadyOptimalMessage, healthCheckMessage]
                        .filter((a) => a)
                        .join("\n\n");

                    if (message) {
                        setTimeout(() => alert(message), 200);
                    }

                    if (!alreadyOptimal) {
                        const comparisonIncludeSameDayReviews =
                            includeSameDayOverrideForComparison(params, resp.params);
                        const currentMetrics =
                            await evaluateParamsLegacyForOptimization("current", {
                                search: evaluateSearch,
                                ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                                params,
                                includeSameDayReviews: comparisonIncludeSameDayReviews,
                            });
                        const optimizedMetrics =
                            await evaluateParamsLegacyForOptimization("optimized", {
                                search: evaluateSearch,
                                ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                                params: resp.params,
                                includeSameDayReviews: comparisonIncludeSameDayReviews,
                            });
                        optimizationComparison = {
                            optimizedParams: [...resp.params],
                            search: evaluateSearch,
                            ignoreRevlogsBeforeMs: getIgnoreRevlogsBeforeMs(),
                            includeSameDayReviews: comparisonIncludeSameDayReviews,
                            current: {
                                logLoss: currentMetrics.logLoss,
                                rmseBins: currentMetrics.rmseBins,
                            },
                            optimized: {
                                logLoss: optimizedMetrics.logLoss,
                                rmseBins: optimizedMetrics.rmseBins,
                            },
                        };
                    }
                    if (computeParamsProgress) {
                        computeParamsProgress.current = computeParamsProgress.total;
                    }
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
            computingParams = false;
        }
    }

    function closeOptimizationComparison(): void {
        optimizationComparison = undefined;
        customDecayRows = [];
        loadingCustomDecayTable = false;
    }

    function keepCurrentParams(): void {
        closeOptimizationComparison();
    }

    function applyOptimizedParams(): void {
        if (!optimizationComparison) {
            return;
        }
        setSelectedFsrsParams(optimizationComparison.optimizedParams);
        optimized = true;
        closeOptimizationComparison();
    }

    async function loadCustomDecayTable(): Promise<void> {
        if (
            !optimizationComparison ||
            loadingCustomDecayTable ||
            !supportsCustomDecayTable(optimizationComparison.optimizedParams)
        ) {
            return;
        }
        loadingCustomDecayTable = true;
        try {
            const comparison = optimizationComparison;
            const rows = await Promise.all(
                customDecayCandidates.map(async (decay) => {
                    const resp = await evaluateParamsLegacy({
                        search: comparison.search,
                        ignoreRevlogsBeforeMs: comparison.ignoreRevlogsBeforeMs,
                        params: withLastParam(comparison.optimizedParams, decay),
                        includeSameDayReviews: comparison.includeSameDayReviews,
                    });
                    return {
                        decay,
                        isBaseline: false,
                        logLoss: resp.logLoss,
                        rmseBins: resp.rmseBins,
                        logLossDelta: metricDelta(
                            comparison.optimized.logLoss,
                            resp.logLoss,
                        ),
                        logLossDeltaPercent: metricDeltaPercent(
                            comparison.optimized.logLoss,
                            resp.logLoss,
                        ),
                        rmseDelta: metricDelta(
                            comparison.optimized.rmseBins,
                            resp.rmseBins,
                        ),
                        rmseDeltaPercent: metricDeltaPercent(
                            comparison.optimized.rmseBins,
                            resp.rmseBins,
                        ),
                    };
                }),
            );
            const optimizedDecay =
                comparison.optimizedParams[comparison.optimizedParams.length - 1] ?? 0;
            customDecayRows = [
                {
                    decay: optimizedDecay,
                    isBaseline: true,
                    logLoss: optimizationComparison.optimized.logLoss,
                    rmseBins: optimizationComparison.optimized.rmseBins,
                    logLossDelta: 0,
                    logLossDeltaPercent: 0,
                    rmseDelta: 0,
                    rmseDeltaPercent: 0,
                },
                ...rows,
            ];
        } finally {
            loadingCustomDecayTable = false;
        }
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
        const params = selectedFsrsParams($config);
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
                            includeSameDayReviews:
                                includeSameDayOverrideForParams(params),
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
        const params = selectedFsrsParams($config);
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
                            fsrsVersion: $config.fsrsVersion,
                            includeSameDayReviews: includeSameDayOverride(),
                            includeSameDayReviewsForTraining: includeSameDayOverride(),
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
        const incompatiblePresetNames = state.incompatibleFsrsParamPresetNames();
        if (incompatiblePresetNames.length) {
            const shownPresets = incompatiblePresetNames
                .slice(0, 8)
                .map((name) => `- ${name}`)
                .join("\n");
            const remaining = incompatiblePresetNames.length - 8;
            const remainingText = remaining > 0 ? `\n- ...and ${remaining} more` : "";
            const shouldClear = confirm(
                [
                    "Some presets have incompatible FSRS parameters. Optimize All Presets needs to clear those fields first so the default parameters can be used.",
                    `Affected presets:\n${shownPresets}${remainingText}`,
                    "Clear the incompatible FSRS parameters and continue?",
                ].join("\n\n"),
            );
            if (!shouldClear) {
                return;
            }
            state.clearIncompatibleFsrsParams();
        }
        state.save(UpdateDeckConfigsMode.COMPUTE_ALL_PARAMS);
    }

    function showSimulatorModal(modal: Modal) {
        const params = selectedFsrsParams($config);
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
    $: outdatedFsrs7ParamsWarning =
        $config.fsrsVersion === DeckConfig_Config_FsrsVersion.SEVEN &&
        fsrsParamDiagnostics($config.fsrsParams7).outdatedFsrs7PreviewParams
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
            bind:focused={desiredRetentionFocused}
        >
            <TabbedValue
                slot="tabs"
                tabs={desiredRetentionTabs}
                bind:value={effectiveDesiredRetention}
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
    <Warning
        warning={desiredRetentionChangeInfo}
        className={desiredRetentionChangeClass}
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

<!-- Changing desired retention moves due dates under every algorithm, so
     the switch is not FSRS-only (spec deck-options.reschedule-on-change).
     It is Advanced-only, like the optimize buttons and the FSRS advanced
     section below (spec deck-options.simple-view). -->
{#if $advanced}
    <SwitchRow bind:value={$fsrsReschedule} defaultValue={false}>
        <SettingTitle on:click={() => openHelpModal("rescheduleCardsOnChange")}>
            <GlobalLabel title={tr.deckConfigRescheduleCardsOnChange()} />
        </SettingTitle>
    </SwitchRow>

    {#if $fsrsReschedule}
        <Warning warning={tr.deckConfigRescheduleCardsWarning()} />
    {/if}
{/if}

{#if !rwkvMode && $advanced}
    <div class="ms-1 me-1">
        <button
            class="btn {computingParams ? 'btn-warning' : 'btn-primary'}"
            disabled={!computingParams && computing}
            on:click={() => computeParams()}
        >
            {#if computingParams}
                {tr.actionsCancel()}
            {:else}
                {tr.deckConfigOptimizeButton()}
            {/if}
        </button>
        <button class="btn btn-primary" on:click={() => computeAllParams()}>
            {tr.deckConfigSaveAndOptimize()}
        </button>
        <div>
            {#if computingParams || checkingParams || checkingHealth}
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

        <div class="mb-3">
            <SettingTitle>{tr.deckConfigFsrsVersion()}</SettingTitle>
            <select bind:value={$config.fsrsVersion} class="form-select">
                {#each fsrsVersionChoices as choice}
                    <option value={choice.value}>{choice.label}</option>
                {/each}
            </select>
        </div>

        {#if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.SIX}
            <ParamsInputRow bind:value={$config.fsrsParams6} defaultValue={[]}>
                <SettingTitle on:click={() => openHelpModal("modelParams")}>
                    {tr.deckConfigWeights()}
                </SettingTitle>
            </ParamsInputRow>
        {:else if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.FIVE}
            <ParamsInputRow bind:value={$config.fsrsParams5} defaultValue={[]}>
                <SettingTitle on:click={() => openHelpModal("modelParams")}>
                    {tr.deckConfigWeights()}
                </SettingTitle>
            </ParamsInputRow>
        {:else if $config.fsrsVersion === DeckConfig_Config_FsrsVersion.FOUR}
            <ParamsInputRow bind:value={$config.fsrsParams4} defaultValue={[]}>
                <SettingTitle on:click={() => openHelpModal("modelParams")}>
                    {tr.deckConfigWeights()}
                </SettingTitle>
            </ParamsInputRow>
        {:else}
            <ParamsInputRow bind:value={$config.fsrsParams7} defaultValue={[]}>
                <SettingTitle on:click={() => openHelpModal("modelParams")}>
                    {tr.deckConfigWeights()}
                </SettingTitle>
            </ParamsInputRow>
        {/if}

        <ParamsSearchRow
            bind:value={$config.paramSearch}
            placeholder={defaultparamSearch}
        >
            <SettingTitle>Search Filter</SettingTitle>
        </ParamsSearchRow>

        <SwitchRow bind:value={$healthCheck} defaultValue={false}>
            <SettingTitle on:click={() => openHelpModal("healthCheck")}>
                <GlobalLabel
                    title={tr.deckConfigSlowSuffix({
                        text: tr.deckConfigHealthCheck(),
                    })}
                />
            </SettingTitle>
        </SwitchRow>

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

{#if optimizationComparison}
    {@const logLossDelta = metricDelta(
        optimizationComparison.current.logLoss,
        optimizationComparison.optimized.logLoss,
    )}
    {@const rmseDelta = metricDelta(
        optimizationComparison.current.rmseBins,
        optimizationComparison.optimized.rmseBins,
    )}
    {@const logLossDeltaPercent = metricDeltaPercent(
        optimizationComparison.current.logLoss,
        optimizationComparison.optimized.logLoss,
    )}
    {@const rmseDeltaPercent = metricDeltaPercent(
        optimizationComparison.current.rmseBins,
        optimizationComparison.optimized.rmseBins,
    )}
    <div class="optimization-popup-backdrop">
        <div class="optimization-popup">
            <div class="optimization-popup-header">Optimization Result</div>
            <table class="optimization-popup-table">
                <thead>
                    <tr>
                        <th>Metric</th>
                        <th>Current</th>
                        <th>Optimized</th>
                        <th>Delta</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <th>Log loss</th>
                        <td>{formatMetric(optimizationComparison.current.logLoss)}</td>
                        <td>
                            {formatMetric(optimizationComparison.optimized.logLoss)}
                        </td>
                        <td class={`optimize-delta ${deltaClass(logLossDelta)}`}>
                            {formatDelta(logLossDelta)}
                            ({formatPercentDelta(logLossDeltaPercent)})
                        </td>
                    </tr>
                    <tr>
                        <th>RMSE (bins)</th>
                        <td>{formatMetric(optimizationComparison.current.rmseBins)}</td>
                        <td>
                            {formatMetric(optimizationComparison.optimized.rmseBins)}
                        </td>
                        <td class={`optimize-delta ${deltaClass(rmseDelta)}`}>
                            {formatDelta(rmseDelta)}
                            ({formatPercentDelta(rmseDeltaPercent)})
                        </td>
                    </tr>
                </tbody>
            </table>
            <div class="optimization-popup-actions">
                <button
                    class="btn btn-outline-primary"
                    disabled={loadingCustomDecayTable ||
                        !supportsCustomDecayTable(
                            optimizationComparison.optimizedParams,
                        )}
                    on:click={loadCustomDecayTable}
                >
                    {#if !supportsCustomDecayTable(optimizationComparison.optimizedParams)}
                        Custom Decay Table (FSRS-7 unsupported)
                    {:else if loadingCustomDecayTable}
                        {tr.actionsProcessing()}
                    {:else}
                        Load Custom Decay Table
                    {/if}
                </button>
            </div>
            {#if customDecayRows.length}
                <table class="optimization-popup-table">
                    <thead>
                        <tr>
                            <th>Decay</th>
                            <th>Log loss</th>
                            <th>Delta</th>
                            <th>RMSE (bins)</th>
                            <th>Delta</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each customDecayRows as row}
                            <tr>
                                <th>
                                    {formatDecay(row.decay)}
                                    {#if row.isBaseline}
                                        (optimized)
                                    {/if}
                                </th>
                                <td>{formatMetric(row.logLoss)}</td>
                                <td
                                    class={`optimize-delta ${deltaClass(row.logLossDelta)}`}
                                >
                                    {formatDelta(row.logLossDelta)}
                                    ({formatPercentDelta(row.logLossDeltaPercent)})
                                </td>
                                <td>{formatMetric(row.rmseBins)}</td>
                                <td
                                    class={`optimize-delta ${deltaClass(row.rmseDelta)}`}
                                >
                                    {formatDelta(row.rmseDelta)}
                                    ({formatPercentDelta(row.rmseDeltaPercent)})
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
            <div class="optimization-popup-footer">
                <button class="btn btn-secondary" on:click={keepCurrentParams}>
                    Keep Current
                </button>
                <button class="btn btn-primary" on:click={applyOptimizedParams}>
                    Use Optimized
                </button>
            </div>
        </div>
    </div>
{/if}

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

    :global(.two-line) {
        white-space: pre-wrap;
        min-height: calc(2ch + 30px);
        box-sizing: content-box;
        display: flex;
        align-content: center;
        flex-wrap: wrap;
    }

    .optimization-popup-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.45);
        z-index: 1060;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 1rem;
    }

    .optimization-popup {
        width: min(760px, 95vw);
        max-width: calc(100vw - 2rem);
        background: var(--canvas);
        border: 1px solid var(--border);
        border-radius: 0.5rem;
        box-shadow: 0 0.75rem 2.25rem rgba(0, 0, 0, 0.2);
        overflow-x: auto;
    }

    .optimization-popup-header {
        font-weight: 700;
        padding: 0.75rem 1rem 0.5rem;
    }

    .optimization-popup-table {
        width: calc(100% - 2rem);
        margin: 0 1rem 0.75rem;
        border-collapse: collapse;
        font-size: 0.9rem;
    }

    .optimization-popup-table th,
    .optimization-popup-table td {
        border: 1px solid var(--border);
        padding: 0.35rem 0.5rem;
        text-align: right;
        white-space: nowrap;
    }

    .optimization-popup-table th:first-child,
    .optimization-popup-table td:first-child {
        text-align: left;
    }

    .optimization-popup-footer {
        display: flex;
        justify-content: flex-end;
        gap: 0.5rem;
        padding: 0 1rem 1rem;
    }

    .optimization-popup-actions {
        padding: 0 1rem 0.25rem;
    }

    .fsrs-progress {
        width: min(24rem, 100%);
        height: 0.5rem;
        margin-top: 0.35rem;
    }

    .optimize-delta.better {
        color: var(--fg-green, #027a48);
        font-weight: 600;
    }

    .optimize-delta.worse {
        color: var(--fg-red, #b42318);
        font-weight: 600;
    }

    .optimize-delta.equal {
        color: var(--fg, inherit);
    }
</style>
