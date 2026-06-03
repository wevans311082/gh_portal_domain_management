(function () {
    function getCarousel(root) {
        var carousel = root.closest("[data-domain-carousel]");
        if (carousel) return carousel;
        return root.querySelector ? root.querySelector("[data-domain-carousel]") : null;
    }

    function getShell(element) {
        return element.closest(".domain-results-shell") || document;
    }

    function updateCarousel(carousel, nextIndex) {
        if (!carousel) return;

        var track = carousel.querySelector(".domain-carousel-track");
        var slides = Array.prototype.slice.call(carousel.querySelectorAll(".domain-carousel-slide"));
        var shell = getShell(carousel);
        var empty = shell.querySelector(".domain-carousel-empty");
        var prev = shell.querySelector(".domain-carousel-prev");
        var next = shell.querySelector(".domain-carousel-next");

        if (!track || slides.length === 0) {
            if (carousel) carousel.classList.add("hidden");
            if (empty) empty.classList.remove("hidden");
            if (prev) prev.disabled = true;
            if (next) next.disabled = true;
            return;
        }

        carousel.classList.remove("hidden");
        if (empty) empty.classList.add("hidden");

        var index = Number(carousel.getAttribute("data-index") || "0");
        if (typeof nextIndex === "number") {
            index = nextIndex;
        }
        if (index < 0) index = slides.length - 1;
        if (index >= slides.length) index = 0;
        carousel.setAttribute("data-index", String(index));

        var offset = slides[index].offsetLeft;
        track.style.transform = "translateX(-" + offset + "px)";

        var disabled = slides.length <= 1;
        [prev, next].forEach(function (button) {
            if (!button) return;
            button.disabled = disabled;
            button.classList.toggle("opacity-40", disabled);
            button.classList.toggle("cursor-not-allowed", disabled);
        });
    }

    function initCarousels(scope) {
        var root = scope || document;
        root.querySelectorAll("[data-domain-carousel]").forEach(function (carousel) {
            updateCarousel(carousel);
        });
    }

    document.addEventListener("click", function (event) {
        var prev = event.target.closest(".domain-carousel-prev");
        var next = event.target.closest(".domain-carousel-next");
        var toggle = event.target.closest(".domain-detail-toggle");

        if (prev || next) {
            var shell = getShell(prev || next);
            var carousel = getCarousel(shell);
            if (!carousel) return;
            var current = Number(carousel.getAttribute("data-index") || "0");
            updateCarousel(carousel, current + (next ? 1 : -1));
            return;
        }

        if (toggle) {
            var id = toggle.getAttribute("aria-controls");
            var panel = id ? document.getElementById(id) : null;
            if (!panel) return;

            var isOpen = toggle.getAttribute("aria-expanded") === "true";
            toggle.setAttribute("aria-expanded", isOpen ? "false" : "true");
            panel.classList.toggle("hidden", isOpen);

            var icon = toggle.querySelector(".domain-detail-icon");
            if (icon) icon.textContent = isOpen ? "+" : "-";
        }
    });

    document.addEventListener("DOMContentLoaded", function () {
        initCarousels(document);
    });

    document.addEventListener("htmx:afterSwap", function (event) {
        initCarousels(event.target);
    });

    window.addEventListener("resize", function () {
        initCarousels(document);
    });
})();
