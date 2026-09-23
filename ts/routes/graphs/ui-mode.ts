// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { GraphsRequest_Graph as Graph } from "@generated/anki/stats_pb";
import { itemShown, type SimpleItems } from "@tslib/ui-split";

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

/** A graph of the Stats page as the split knows it: its item id after
 * "stats." (qt/aqt/ui_split.py) and the data it draws from the page's graphs
 * request (none for a graph that asks for its own). */
export interface GraphItem {
    id: string;
    data: Graph[];
    /** The graph names retrievability, so only Advanced mode draws it,
     * whatever the split says; it is no item of the split (spec
     * ui.retrievability-advanced-only). */
    advancedOnly?: boolean;
}

/** The split's item id of a graph. */
export function statsItemId(item: GraphItem): string {
    return `stats.${item.id}`;
}

/** The graphs Simple mode shows (spec ui.split-configurable, whose defaults
 * are Reviews, Card Counts, Retention and Total Knowledge), in page order. */
export function simpleGraphsOf<T>(graphs: T[], items: GraphItem[], simpleItems: SimpleItems | null): T[] {
    return graphs.filter((_, index) => simpleShows(items[index], simpleItems));
}

/** Whether Simple mode draws a graph: never an Advanced-only one, even when
 * the split cannot be read. */
function simpleShows(item: GraphItem, simpleItems: SimpleItems | null): boolean {
    return !item.advancedOnly && itemShown(statsItemId(item), false, simpleItems);
}

/**
 * The data Simple mode asks the backend for: what its graphs draw. Without
 * the split every graph shows, so everything is asked for ([]). When the
 * Simple graphs draw nothing of it (each asks for its own data), one small
 * part is asked for, since an empty list means every graph. The order is
 * page order, so the default request is the same as before the split.
 */
export function simpleDataOf(items: GraphItem[], simpleItems: SimpleItems | null): Graph[] {
    if (simpleItems === null) {
        return [];
    }
    const wanted = new Set<Graph>();
    for (const item of items) {
        if (simpleShows(item, simpleItems)) {
            item.data.forEach((graph) => wanted.add(graph));
        }
    }
    if (wanted.size === 0) {
        return [Graph.CARD_COUNTS];
    }
    return [...wanted];
}
