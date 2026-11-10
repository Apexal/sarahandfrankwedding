// The seating editor at /manage/seating/ (guests/views/seating.py). The dialog and the error banner come
// from guest-list.js, loaded on the same page.
//
// - `[data-seat-list]` lists (each table's guests, and the pool's) share the "seats" group: dropping a
//   guest posts its id with the list's `data-table` (blank = unseat). They also accept pool households
//   (`[data-seat-household]`, dragged by `.seat-household-handle`), which post every id in
//   `data-guest-ids`. The server answers with the whole board, which replaces #seating-board, so the DOM
//   Sortable rearranged is thrown away either way.
// - `[data-seat-tables]` is the table grid, dragged by `.seat-table-handle`; a reorder posts every id.
// - `[data-seat-filter]` inputs (outside the board, so they survive a swap) filter the pool in place.
(function () {
    "use strict";

    var BOARD = "#seating-board";

    function headers() {
        try {
            return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
        } catch (e) {
            return {};
        }
    }

    function board() {
        return document.querySelector(BOARD);
    }

    function post(url, values, source, swapBoard) {
        return htmx.ajax("POST", url, {
            source: source,
            values: values,
            headers: headers(),
            target: swapBoard ? BOARD : "#gl-modal-body",
            swap: swapBoard ? "outerHTML" : "innerHTML",
        });
    }

    function assign(item, list) {
        var ids = item.dataset.guestIds ? item.dataset.guestIds.split(" ") : [item.dataset.id];
        post(board().dataset.assignUrl, { guest: ids, table: list.dataset.table }, list, true);
    }

    var SORTABLE_DEFAULTS = {
        animation: 150,
        delay: 150,
        delayOnTouchOnly: true,
        // Pointer events rather than native HTML5 drag: the same in every browser (see guest-list.js).
        forceFallback: true,
        fallbackOnBody: true,
        ghostClass: "opacity-40",
    };

    function create(el, options) {
        if (Sortable.get(el)) return;
        Sortable.create(el, Object.assign({}, SORTABLE_DEFAULTS, options));
    }

    function initBoard(root) {
        root.querySelectorAll("[data-seat-list]").forEach(function (list) {
            create(list, {
                group: { name: "seats", pull: true, put: ["seats", "households"] },
                draggable: "[data-seat-guest]",
                sort: false,
                onAdd: function (event) {
                    assign(event.item, event.to);
                },
            });
        });
        root.querySelectorAll("[data-seat-pool]").forEach(function (pool) {
            create(pool, {
                group: { name: "households", pull: true, put: false },
                draggable: "[data-seat-household]",
                handle: ".seat-household-handle",
                sort: false,
            });
        });
        root.querySelectorAll("[data-seat-tables]").forEach(function (grid) {
            create(grid, {
                group: "tables",
                draggable: "[data-seat-table]",
                handle: ".seat-table-handle",
                onEnd: function (event) {
                    if (event.oldIndex === event.newIndex) return;
                    var order = Array.prototype.map.call(grid.querySelectorAll(":scope > [data-seat-table]"), function (el) {
                        return el.dataset.id;
                    });
                    // 204 on success; a stale order comes back as the board, re-rendered.
                    htmx.ajax("POST", grid.dataset.reorderUrl, {
                        source: grid,
                        values: { order: order },
                        headers: headers(),
                        target: BOARD,
                        swap: "outerHTML",
                    });
                },
            });
        });
    }

    // --- The pool filter -----------------------------------------------------------------------

    function filterValue(name) {
        var input = document.querySelector('[data-seat-filter="' + name + '"]');
        if (!input) return "";
        return input.type === "checkbox" ? input.checked : input.value.trim().toLowerCase();
    }

    function applyFilter() {
        var q = filterValue("q");
        var attendingOnly = filterValue("attending");
        var shown = 0;
        document.querySelectorAll("[data-seat-pool] [data-seat-household]").forEach(function (household) {
            var visibleGuests = 0;
            household.querySelectorAll("[data-seat-guest]").forEach(function (chip) {
                var hide = attendingOnly && chip.dataset.status !== "attending";
                chip.classList.toggle("hidden", hide);
                if (!hide) visibleGuests += 1;
            });
            var match = !q || household.dataset.search.indexOf(q) !== -1;
            var show = match && visibleGuests > 0;
            household.classList.toggle("hidden", !show);
            if (show) shown += 1;
        });
        var empty = document.querySelector("[data-seat-pool-empty]");
        var any = document.querySelector("[data-seat-pool] [data-seat-household]");
        if (empty) empty.classList.toggle("hidden", shown > 0 || !any);
    }

    document.addEventListener("input", function (event) {
        if (event.target.closest("[data-seat-filter]")) applyFilter();
    });

    htmx.onLoad(function (root) {
        if (typeof Sortable === "undefined" || !root.querySelectorAll) return;
        var target = root.matches && root.matches(BOARD) ? root : root.querySelector(BOARD);
        if (!target) return;
        initBoard(target);
        applyFilter();
    });
})();
