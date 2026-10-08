// The save-the-date envelope (static/css/save-the-date.css has the states and the timeline).
// Loaded synchronously in <head> so `html.js` is set before the stage is painted; without this
// script the letter renders as an ordinary page.
(function () {
    "use strict";

    document.documentElement.classList.add("js");

    var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

    // A face's natural height: its content plus its own vertical padding. Layout sizes ignore the
    // letter's scale transform, so this is the full-size height even while the letter is shrunk.
    function naturalHeight(face) {
        var style = window.getComputedStyle(face);
        var content = face.firstElementChild ? face.firstElementChild.offsetHeight : 0;
        return Math.ceil(content + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom));
    }

    // Size the three panels to their content, so the folded layout and the static one it hands
    // over to at the end are the same height. Panel one keeps the stylesheet's minimum.
    function measure(stage) {
        stage.style.removeProperty("--p1");
        var minimum = parseFloat(window.getComputedStyle(stage).getPropertyValue("--p1")) || 0;
        var panels = [
            ["--p1", stage.querySelector(".letter-panel-1"), minimum],
            ["--p2", stage.querySelector(".letter-fold-2 > .letter-face"), 0],
            ["--p3", stage.querySelector(".letter-fold-3 > .letter-face"), 0],
        ];
        panels.forEach(function (panel) {
            if (panel[1]) {
                stage.style.setProperty(panel[0], Math.max(panel[2], naturalHeight(panel[1])) + "px");
            }
        });
    }

    function finish(stage) {
        stage.classList.remove("is-sealed", "is-open");
        stage.classList.add("is-done");
        var title = stage.querySelector("#letter-title");
        if (title) {
            title.focus({ preventScroll: true });
        }
    }

    // The floating "Scroll" cue: shown once the lettering is written (or straight away when there is
    // no animation), unless the guest has already scrolled, and faded out at their first scroll.
    function showScrollCue(stage) {
        var cue = document.querySelector("[data-scroll-cue]");
        if (!cue || !cue.hidden || cue.dataset.shown) {
            return;
        }
        cue.dataset.shown = "true";
        var start = window.scrollY;
        if (start > 40) {
            return;
        }

        function leave() {
            window.removeEventListener("scroll", onScroll);
            cue.classList.add("is-leaving");
            window.setTimeout(function () {
                cue.hidden = true;
            }, 400);
        }
        function onScroll() {
            if (Math.abs(window.scrollY - start) > 24) {
                leave();
            }
        }

        cue.addEventListener("click", function () {
            // The date, city, and calendar link under the photo, centred in the window.
            var next = stage.querySelector("[data-std-details]");
            if (next) {
                next.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "center" });
            }
            leave();
        });
        cue.hidden = false;
        // Let the un-hidden button paint at opacity 0 first, so the fade-in transition runs.
        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
                cue.classList.add("is-visible");
            });
        });
        window.addEventListener("scroll", onScroll, { passive: true });
    }

    function init() {
        var stage = document.querySelector("[data-envelope].is-sealed");
        if (!stage) {
            return;
        }
        var button = stage.querySelector(".env-hit");
        var letter = stage.querySelector(".letter");

        measure(stage);
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(function () {
                if (stage.classList.contains("is-sealed")) {
                    measure(stage);
                }
            });
        }
        window.addEventListener("resize", function () {
            if (stage.classList.contains("is-sealed")) {
                measure(stage);
            }
        });

        letter.addEventListener("animationend", function (event) {
            if (event.target === letter && event.animationName === "std-letter") {
                finish(stage);
            }
        });

        // Frank's name is the last thing written (the lettering timeline in save-the-date.css).
        stage.addEventListener("animationend", function (event) {
            if (event.animationName === "lt-wipe" && event.target.closest("#lt-wipe-frank")) {
                showScrollCue(stage);
            }
        });

        button.addEventListener("click", function () {
            if (reducedMotion.matches) {
                finish(stage);
                showScrollCue(stage);
                return;
            }
            // In case a browser fires no animationend inside an SVG mask: the lettering is done ~10s in.
            window.setTimeout(function () {
                showScrollCue(stage);
            }, 10500);
            measure(stage);
            stage.classList.remove("is-sealed");
            // `is-writing` outlives `is-open`, so the lettering keeps drawing after the hand-off to `is-done`.
            stage.classList.add("is-open", "is-writing");
        });
    }

    // The address form posts with HTMX and swaps in the form again (with errors) or the thank-you,
    // keeping the scroll position. Move focus to what came back without jumping the page; role=alert
    // on the error summary announces it.
    document.addEventListener("htmx:afterSwap", function (event) {
        if (event.detail.target.id !== "std-reply") {
            return;
        }
        var reply = document.getElementById("std-reply");
        var target = reply && reply.querySelector("#form-errors, #thank-you h2");
        if (target) {
            target.focus({ preventScroll: true });
        }
    });

    // A response HTMX will not swap (the rate limit's 429, a server error) or a failed request:
    // post the form the ordinary way, so the guest sees the real page instead of nothing.
    function fallBack(event) {
        var form = event.detail.elt;
        if (form && form.hasAttribute && form.hasAttribute("data-address-form")) {
            form.submit();
        }
    }
    document.addEventListener("htmx:responseError", fallBack);
    document.addEventListener("htmx:sendError", fallBack);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
