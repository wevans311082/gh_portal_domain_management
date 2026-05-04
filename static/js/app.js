// Grumpy Hosting Portal - Application JavaScript
console.log('Grumpy Hosting Portal loaded');

(function () {
    function dismissToast(el) {
        el.classList.add('opacity-0', 'translate-x-4');
        setTimeout(function () { el.remove(); }, 260);
    }

    document.querySelectorAll('[data-toast]').forEach(function (toast) {
        var timeout = parseInt(toast.getAttribute('data-timeout') || '5000', 10);
        var closeBtn = toast.querySelector('.toast-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', function () {
                dismissToast(toast);
            });
        }
        setTimeout(function () {
            if (document.body.contains(toast)) {
                dismissToast(toast);
            }
        }, timeout);
    });

    var scopedRoot = document.querySelector('.admin-shell, .portal-shell, .auth-shell');
    if (!scopedRoot) {
        return;
    }

    scopedRoot.querySelectorAll('form').forEach(function (formEl) {
        if (formEl.closest('nav') || formEl.closest('header') || formEl.hasAttribute('data-no-form-card')) {
            return;
        }
        if (formEl.querySelector('input:not([type="hidden"]), select, textarea')) {
            formEl.classList.add('form-surface');
        }
    });

    function labelForField(field) {
        if (field.id) {
            var explicit = scopedRoot.querySelector('label[for="' + field.id + '"]');
            if (explicit) {
                return explicit.textContent.trim();
            }
        }
        var parent = field.closest('div, td, section, fieldset, form');
        if (!parent) {
            return '';
        }
        var fallback = parent.querySelector('label');
        return fallback ? fallback.textContent.trim() : '';
    }

    function exampleTextForField(field, labelText) {
        var fieldName = (field.getAttribute('name') || '').toLowerCase();
        var fieldType = (field.getAttribute('type') || '').toLowerCase();
        var tag = field.tagName.toLowerCase();

        if (tag === 'select') {
            return 'Example: Select the most appropriate option.';
        }
        if (fieldType === 'email' || fieldName.indexOf('email') !== -1) {
            return 'Example: name@example.com';
        }
        if (fieldType === 'tel' || fieldName.indexOf('phone') !== -1) {
            return 'Example: +44 7700 900123';
        }
        if (fieldType === 'number' || fieldName.indexOf('amount') !== -1 || fieldName.indexOf('price') !== -1) {
            return 'Example: 99.99';
        }
        if (fieldType === 'date') {
            return 'Example: 2026-05-04';
        }
        if (fieldType === 'url' || fieldName.indexOf('url') !== -1 || fieldName.indexOf('website') !== -1) {
            return 'Example: https://example.com';
        }
        if (fieldType === 'password') {
            return 'Example: At least 12 characters with mixed types.';
        }
        if (fieldName.indexOf('postcode') !== -1 || fieldName.indexOf('zip') !== -1) {
            return 'Example: SW1A 1AA';
        }
        if (fieldName.indexOf('country') !== -1) {
            return 'Example: GB (2-letter ISO code)';
        }
        if (fieldName.indexOf('company_number') !== -1) {
            return 'Example: 00445790';
        }
        if (fieldName.indexOf('domain') !== -1) {
            return 'Example: example.co.uk';
        }
        if (fieldName.indexOf('city') !== -1) {
            return 'Example: London';
        }
        if (fieldName.indexOf('state') !== -1 || fieldName.indexOf('county') !== -1) {
            return 'Example: Greater London';
        }
        if (fieldName.indexOf('name') !== -1) {
            return 'Example: Jane Smith';
        }
        if (tag === 'textarea') {
            return 'Example: Add clear details so support/staff can action this quickly.';
        }
        if (labelText) {
            return 'Example: Enter ' + labelText.replace(/\s+/g, ' ').trim().toLowerCase() + '.';
        }
        return 'Example: Enter the required value.';
    }

    scopedRoot.querySelectorAll('input, select, textarea').forEach(function (field) {
        if (field.dataset.exampleApplied === '1') {
            return;
        }

        var fieldType = (field.getAttribute('type') || '').toLowerCase();
        if (
            fieldType === 'hidden' ||
            fieldType === 'checkbox' ||
            fieldType === 'radio' ||
            fieldType === 'submit' ||
            fieldType === 'button' ||
            fieldType === 'reset'
        ) {
            return;
        }

        var labelText = labelForField(field);
        var example = exampleTextForField(field, labelText);

        if (field.tagName.toLowerCase() !== 'select' && !field.getAttribute('placeholder')) {
            field.setAttribute('placeholder', example.replace(/^Example:\s*/i, ''));
        }

        var next = field.nextElementSibling;
        var hasInjectedHint = next && next.classList && next.classList.contains('field-example-hint');
        if (!hasInjectedHint) {
            var hint = document.createElement('p');
            hint.className = 'field-example-hint';
            hint.textContent = example;
            field.insertAdjacentElement('afterend', hint);
        }

        field.dataset.exampleApplied = '1';
    });
})();
