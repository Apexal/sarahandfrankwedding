// The contact sheet at /manage/contacts/ (guests/views/contacts.py).
//
// - Enter / ArrowDown move to the same column on the next row, Shift+Enter / ArrowUp to the row above,
//   like a spreadsheet. Leaving a cell fires `change`, which is what saves it (HTMX, one field per post).
// - A failed save (server error or no connection) shows `#contacts-error` with a reload button.
(function () {
    "use strict";

    function rowStep(input, step) {
        var row = input.closest("tr");
        var column = input.dataset.col;
        while (row) {
            row = step > 0 ? row.nextElementSibling : row.previousElementSibling;
            var next = row && row.querySelector('[data-col="' + column + '"]');
            if (next) return next;
        }
        return null;
    }

    document.addEventListener("keydown", function (event) {
        var input = event.target;
        if (!(input instanceof HTMLInputElement) || !input.dataset.col || !input.closest("[data-contacts]")) return;
        if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
        var step = 0;
        if (event.key === "Enter") step = event.shiftKey ? -1 : 1;
        else if (event.key === "ArrowDown" && !event.shiftKey) step = 1;
        else if (event.key === "ArrowUp" && !event.shiftKey) step = -1;
        if (!step) return;
        var target = rowStep(input, step);
        event.preventDefault();
        if (target) {
            target.focus();
            if (target.select && target.type !== "checkbox") target.select();
        } else {
            input.blur(); // last row: still save it
        }
    });

    function showError() {
        var banner = document.getElementById("contacts-error");
        if (banner) banner.classList.remove("hidden");
    }

    document.addEventListener("htmx:responseError", showError);
    document.addEventListener("htmx:sendError", showError);
    document.addEventListener("click", function (event) {
        if (event.target.closest("[data-contacts-reload]")) window.location.reload();
    });
})();
