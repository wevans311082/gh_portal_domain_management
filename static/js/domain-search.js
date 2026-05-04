(function () {
    function wireTldButtons() {
        var input = document.getElementById("domain-search-input");
        if (!input) {
            return;
        }

        var filterInput = document.getElementById("tld-filter-input");
        var clearBtn = document.getElementById("tld-clear");
        var buttons = document.querySelectorAll("[data-domain-tld]");

        function syncFilterValue() {
            if (!filterInput) return;
            var active = [];
            buttons.forEach(function (b) {
                if (b.getAttribute("data-active") === "true") {
                    active.push(b.getAttribute("data-domain-tld"));
                }
            });
            filterInput.value = active.join(",");
            if (clearBtn) {
                clearBtn.classList.toggle("hidden", active.length === 0);
            }
            // Re-trigger HTMX search if there's already a query in the box
            if (window.htmx && (input.value || "").trim()) {
                window.htmx.trigger(input, "input");
            }
        }

        buttons.forEach(function (button) {
            button.addEventListener("click", function () {
                var tld = button.getAttribute("data-domain-tld");
                if (!tld) return;

                // If user has typed nothing, fall back to legacy "fill the box"
                // behaviour so chips still feel useful as quick examples.
                var raw = (input.value || "").trim();
                if (!raw) {
                    input.value = "example." + tld;
                    if (window.htmx) window.htmx.trigger(input, "input");
                    return;
                }

                // Otherwise toggle chip as a filter.
                var nowActive = button.getAttribute("data-active") !== "true";
                button.setAttribute("data-active", nowActive ? "true" : "false");
                syncFilterValue();
            });
        });

        if (clearBtn) {
            clearBtn.addEventListener("click", function () {
                buttons.forEach(function (b) { b.setAttribute("data-active", "false"); });
                syncFilterValue();
            });
        }
    }

    document.addEventListener("DOMContentLoaded", wireTldButtons);
})();
