/* Native datalist enhancement; the GET search form also works without JS. */
(function () {
    const form = document.querySelector('[data-admin-search]');
    if (!form) return;
    const input = form.querySelector('[name="q"]');
    const group = form.querySelector('[name="group"]');
    const list = document.getElementById('search-suggestions');
    const status = form.querySelector('[data-search-status]');
    let timer, controller, generation = 0;
    input.addEventListener('input', function () {
        clearTimeout(timer);
        if (controller) controller.abort();
        const current = ++generation;
        const query = input.value.trim();
        list.replaceChildren();
        status.textContent = '';
        if (query.length < 2 || query.length > 255) return;
        timer = setTimeout(async function () {
            controller = new AbortController();
            status.textContent = 'Finding suggestions.';
            try {
                const url = new URL(form.dataset.suggestionsUrl, location.origin);
                url.searchParams.set('q', query);
                if (group.value) url.searchParams.set('group', group.value);
                const response = await fetch(url, {signal: controller.signal, headers: {'Accept': 'application/json'}});
                if (!response.ok) throw new Error('Suggestions unavailable');
                const payload = await response.json();
                if (current !== generation) return;
                (payload.suggestions || []).forEach(item => {
                    const option = document.createElement('option');
                    option.value = item.value;
                    option.label = item.label;
                    list.appendChild(option);
                });
                status.textContent = list.children.length ? 'Suggestions available. Press Enter to search.' : 'No suggestions. You can still submit your search.';
            } catch (error) {
                if (error.name === 'AbortError' || current !== generation) return;
                status.textContent = 'Suggestions are unavailable. Submit the search form to try again.';
            }
        }, 250);
    });
    form.addEventListener('submit', function () { clearTimeout(timer); if (controller) controller.abort(); });
})();
