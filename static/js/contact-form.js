"use strict";

document.addEventListener("DOMContentLoaded", function () {
    const summary = document.querySelector("[data-contact-error-summary]");
    if (!summary) return;
    summary.focus();
    summary.addEventListener("click", function (event) {
        const link = event.target.closest('a[href^="#"]');
        if (!link) return;
        const field = document.getElementById(link.getAttribute("href").slice(1));
        if (!field) return;
        event.preventDefault();
        field.scrollIntoView({block: "center"});
        field.focus({preventScroll: true});
    });
});
