(function () {
    function wireTldButtons() {
        var input = document.getElementById("domain-search-input");
        if (!input) {
            return;
        }

        var buttons = document.querySelectorAll("[data-domain-tld]");
        buttons.forEach(function (button) {
            button.addEventListener("click", function () {
                var tld = button.getAttribute("data-domain-tld");
                var raw = (input.value || "").trim();
                var base = raw.split(".")[0] || "";
                if (!base || !tld) {
                    return;
                }

                input.value = base + "." + tld;
                if (window.htmx) {
                    window.htmx.trigger(input, "input");
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", wireTldButtons);
})();
