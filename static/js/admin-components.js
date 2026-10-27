/* Shared admin shell behavior. Loaded before Alpine initializes the shell. */
window.adminShell = function () {
    return {
        sidebarOpen: false,
        pageTitle: '',
        opener: null,
        media: null,
        previousOverflow: null,
        backgroundNodes: [],
        navigationFocus: false,
        init() {
            this.pageTitle = (this.$refs.pageTitleSource?.textContent || '').trim() || 'Dashboard';
            this.media = window.matchMedia('(min-width: 1024px)');
            this.onResize = () => {
                // CSS can hide the active item before the media event runs.
                const inside = this.sidebarOpen || this.navigationFocus || this.$refs.navigation.contains(document.activeElement);
                this.closeNavigation(false);
                if (inside) {
                    this.$nextTick(() => this.media.matches ? this.focusable()[0]?.focus() : this.$refs.openMenu.focus());
                }
            };
            this.media.addEventListener('change', this.onResize);
            const cookie = document.getElementById('cookie-banner');
            if (cookie) {
                this.cookieObserver = new ResizeObserver(() => {
                    const height = cookie.getBoundingClientRect().height;
                    this.$refs.main.style.setProperty('--admin-bottom-obstruction', (height ? height + 16 : 0) + 'px');
                });
                this.cookieObserver.observe(cookie);
            }
            this.syncNavigation();
        },
        destroy() {
            this.media?.removeEventListener('change', this.onResize);
            this.cookieObserver?.disconnect();
            this.restoreScroll();
            this.restoreBackground();
        },
        focusable() {
            return Array.from(this.$refs.navigation.querySelectorAll('a[href], button, summary, input, select, textarea, [tabindex]'))
                .filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
        },
        syncNavigation() {
            const drawer = this.$refs.navigation;
            const modal = !this.media.matches && this.sidebarOpen;
            drawer.inert = !this.media.matches && !this.sidebarOpen;
            drawer.classList.toggle('mobile-drawer-open', modal);
            drawer.classList.toggle('mobile-drawer-closed', !modal);
            if (drawer.inert) drawer.setAttribute('aria-hidden', 'true');
            else drawer.removeAttribute('aria-hidden');
            if (modal) {
                drawer.setAttribute('role', 'dialog');
                drawer.setAttribute('aria-modal', 'true');
            } else {
                drawer.removeAttribute('role');
                drawer.removeAttribute('aria-modal');
            }
            this.$refs.main.inert = modal;
            this.$refs.skipLink.inert = modal;
        },
        openNavigation(opener) {
            if (this.media.matches || this.sidebarOpen) return;
            this.opener = opener;
            this.sidebarOpen = true;
            this.previousOverflow = {body: document.body.style.overflow, main: this.$refs.main.style.overflow};
            document.body.style.overflow = 'hidden';
            this.$refs.main.style.overflow = 'hidden';
            this.backgroundNodes = Array.from(document.body.children)
                .filter(node => !node.contains(this.$root) && !node.matches('script, style, link'))
                .map(node => ({node: node, inert: node.inert}));
            this.backgroundNodes.forEach(item => { item.node.inert = true; });
            this.syncNavigation();
            this.$nextTick(() => this.$refs.closeMenu.focus({preventScroll: true}));
        },
        restoreScroll() {
            if (!this.previousOverflow) return;
            document.body.style.overflow = this.previousOverflow.body;
            this.$refs.main.style.overflow = this.previousOverflow.main;
            this.previousOverflow = null;
        },
        restoreBackground() {
            this.backgroundNodes.forEach(item => { item.node.inert = item.inert; });
            this.backgroundNodes = [];
        },
        closeNavigation(restoreFocus = true) {
            const wasOpen = this.sidebarOpen;
            this.sidebarOpen = false;
            this.restoreScroll();
            this.restoreBackground();
            this.syncNavigation();
            if (wasOpen && restoreFocus && this.opener?.getClientRects().length) {
                this.opener.focus({preventScroll: true});
            }
        },
        navigationKey(event) {
            if (!this.sidebarOpen || this.media.matches) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                this.closeNavigation();
            } else if (event.key === 'Tab') {
                const items = this.focusable();
                const first = items[0], last = items[items.length - 1];
                if (!first) { event.preventDefault(); this.$refs.navigation.focus(); return; }
                if (event.shiftKey && (document.activeElement === first || !this.$refs.navigation.contains(document.activeElement))) {
                    event.preventDefault(); last.focus();
                } else if (!event.shiftKey && (document.activeElement === last || !this.$refs.navigation.contains(document.activeElement))) {
                    event.preventDefault(); first.focus();
                }
            }
        },
    };
};

document.addEventListener('DOMContentLoaded', function () {
    const summary = document.querySelector('[data-admin-error-summary]');
    if (summary) summary.focus();
    document.addEventListener('click', function (event) {
        const link = event.target.closest('[data-admin-error-summary] a[href^="#"]');
        if (!link) return;
        const field = document.getElementById(link.getAttribute('href').slice(1));
        if (!field) return;
        event.preventDefault();
        field.scrollIntoView({block: 'center'});
        field.focus({preventScroll: true});
    });
    document.addEventListener('submit', function (event) {
        const form = event.target.closest('form[data-admin-submit]');
        if (!form) return;
        if (form.getAttribute('aria-busy') === 'true') { event.preventDefault(); return; }
        const status = document.getElementById(form.dataset.adminSubmit);
        if (status) {
            status.hidden = false;
            status.scrollIntoView({block: 'nearest'});
        }
        // Keep named submit buttons enabled so their action remains in POST.
        form.setAttribute('aria-busy', 'true');
    });
});
window.addEventListener('pageshow', function () {
    document.querySelectorAll('form[data-admin-submit]').forEach(form => {
        form.removeAttribute('aria-busy');
        const status = document.getElementById(form.dataset.adminSubmit);
        if (status) status.hidden = true;
    });
});
