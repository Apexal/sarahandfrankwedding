// The guest list editor at /manage/guests/ (guests/views/guest_list.py).
//
// - SortableJS lists: `[data-gl-list="households"]` (the card grid, dragged by `.gl-household-handle`) and
//   one `[data-gl-list="guests"]` per card (dragged by `.gl-guest-handle`, shared across cards so a guest
//   can be dropped into another household). A reorder posts every visible id as `order`; a drop into
//   another card posts to the guest's `data-move-url`, which may answer with a confirmation dialog.
// - `[data-gl-move]` buttons move an item one place up or down and save the same way.
// - `#gl-modal` is the one dialog. Anything swapped into `#gl-modal-body` opens it; an empty swap or the
//   `gl-close-modal` event closes it. Closing a dialog marked `data-gl-refresh` re-renders those cards,
//   which undoes a drop the person cancelled.
(function () {
    "use strict";

    var MODAL_BODY = "#gl-modal-body";

    function headers() {
        try {
            return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
        } catch (e) {
            return {};
        }
    }

    function idsOf(list) {
        var selector = list.dataset.glList === "households" ? "[data-gl-household]" : "[data-gl-guest]";
        return Array.prototype.filter
            .call(list.children, function (el) {
                return el.matches(selector);
            })
            .map(function (el) {
                return el.dataset.id;
            });
    }

    function post(url, values, source, target, swap) {
        return htmx.ajax("POST", url, {
            source: source,
            values: values,
            headers: headers(),
            target: target || MODAL_BODY,
            swap: swap || "innerHTML",
        });
    }

    function saveOrder(list) {
        if (list.dataset.glList === "households") {
            // 204 on success: nothing to swap.
            post(list.dataset.reorderUrl, { order: idsOf(list) }, list);
        } else {
            // 204 on success; a stale list comes back as the card, re-rendered.
            post(list.dataset.reorderUrl, { order: idsOf(list) }, list, "#household-" + list.dataset.household, "outerHTML");
        }
    }

    function initList(list) {
        if (Sortable.get(list)) return;
        var households = list.dataset.glList === "households";
        Sortable.create(list, {
            group: households ? "households" : "guests",
            handle: households ? ".gl-household-handle" : ".gl-guest-handle",
            draggable: households ? "[data-gl-household]" : "[data-gl-guest]",
            animation: 150,
            delay: 150,
            delayOnTouchOnly: true,
            // Pointer events rather than native HTML5 drag: the same behaviour in every browser, and the
            // dragged card follows the cursor instead of a translucent browser snapshot.
            forceFallback: true,
            fallbackOnBody: true,
            ghostClass: "opacity-40",
            onEnd: function (event) {
                if (event.from === event.to) {
                    if (event.oldIndex !== event.newIndex) saveOrder(event.to);
                    return;
                }
                post(event.item.dataset.moveUrl, { household: event.to.dataset.household, order: idsOf(event.to) }, event.to);
            },
        });
    }

    htmx.onLoad(function (root) {
        if (typeof Sortable === "undefined" || !root.querySelectorAll) return;
        if (root.matches && root.matches("[data-gl-list]")) initList(root);
        root.querySelectorAll("[data-gl-list]").forEach(initList);
    });

    // Up/down buttons: the keyboard and phone way to reorder.
    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-gl-move]");
        if (!button) return;
        var kind = button.dataset.glMove.split("-");
        var selector = kind[0] === "household" ? "[data-gl-household]" : "[data-gl-guest]";
        var item = button.closest(selector);
        var list = item && item.parentElement;
        if (!list) return;
        var sibling = kind[1] === "up" ? item.previousElementSibling : item.nextElementSibling;
        if (!sibling || !sibling.matches(selector)) return;
        if (kind[1] === "up") {
            list.insertBefore(item, sibling);
        } else {
            list.insertBefore(sibling, item);
        }
        button.focus();
        item.scrollIntoView({ block: "nearest" });
        saveOrder(list);
    });

    // --- The dialog --------------------------------------------------------------------------

    function dialog() {
        return document.getElementById("gl-modal");
    }

    function closeDialog() {
        var modal = dialog();
        if (modal && modal.open) modal.close();
    }

    document.addEventListener("htmx:afterSwap", function (event) {
        if (!event.detail.target || event.detail.target.id !== "gl-modal-body") return;
        var modal = dialog();
        if (event.detail.target.innerHTML.trim()) {
            if (!modal.open) modal.showModal();
            var focus = event.detail.target.querySelector("[autofocus]");
            if (focus) focus.focus();
        } else {
            closeDialog();
        }
    });

    document.addEventListener("gl-close-modal", closeDialog);

    document.addEventListener("click", function (event) {
        if (event.target.closest("[data-gl-close]")) closeDialog();
        if (event.target.closest("[data-gl-reload]")) window.location.reload();
    });

    document.addEventListener("DOMContentLoaded", function () {
        var modal = dialog();
        if (!modal) return;
        modal.addEventListener("close", function () {
            var body = document.querySelector(MODAL_BODY);
            var refresh = body.querySelector("[data-gl-refresh]");
            if (refresh) {
                refresh.dataset.glRefresh.split(" ").forEach(function (id) {
                    var card = document.getElementById("household-" + id);
                    if (card) htmx.ajax("GET", card.dataset.cardUrl, { target: card, swap: "outerHTML" });
                });
            }
            body.innerHTML = "";
        });
    });

    // A failed write leaves the page out of step with the database: say so.
    function showError() {
        closeDialog();
        var alert = document.getElementById("gl-error");
        if (alert) alert.classList.remove("hidden");
    }
    document.addEventListener("htmx:responseError", showError);
    document.addEventListener("htmx:sendError", showError);
})();
