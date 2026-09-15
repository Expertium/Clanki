/* Copyright: Ankitects Pty Ltd and contributors
 * License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html */

$(init);

function init() {
    $("tr.deck").draggable({
        scroll: false,

        // can't use "helper: 'clone'" because of a bug in jQuery 1.5
        helper: function(_event) {
            return $(this).clone(false);
        },
        delay: 200,
        opacity: 0.7,
    });
    $("tr.deck").droppable({
        drop: handleDropEvent,
        hoverClass: "drag-hover",
    });
    $("tr.top-level-drag-row").droppable({
        drop: handleDropEvent,
        hoverClass: "drag-hover",
    });
}

function handleDropEvent(event, ui) {
    const draggedDeckId = ui.draggable.attr("id");
    const ontoDeckId = $(this).attr("id") || "";

    pycmd("drag:" + draggedDeckId + "," + ontoDeckId);
}

/** Replace the rows of the deck table (after a collapse or expand) without
 * reloading the page; deckbrowser.py falls back to a reload when this could
 * give a different page. */
function replaceDeckTree(html: string) {
    const table = document.getElementById("decktree");
    if (!table) {
        return;
    }
    table.innerHTML = html;
    init();
}
