// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The graphs the Stats page shows (spec ui.mode-switch): every graph in
 * Advanced mode; in Simple mode only those in `simpleGraphs`, in page order.
 * A page without a Simple list (`null`) shows every graph in both modes.
 */
export function graphsForMode<T>(graphs: T[], simpleGraphs: T[] | null, advanced: boolean): T[] {
    if (advanced || simpleGraphs === null) {
        return graphs;
    }
    return graphs.filter((graph) => simpleGraphs.includes(graph));
}
