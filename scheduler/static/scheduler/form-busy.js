/*
 * Show that a slow form is working, and stop it being submitted twice.
 *
 * Opt in by putting data-busy on the form. The button keeps a spinner and a
 * label to swap to:
 *
 *   <form method="post" data-busy>
 *     <button type="submit" data-busy-label="Signing in...">
 *       <span class="btn-spinner" aria-hidden="true"></span>
 *       <span class="btn-label">Sign in</span>
 *     </button>
 *
 * The button is deliberately never disabled. A disabled control is left out of
 * the submitted data, and several forms here tell their actions apart by which
 * button's name arrives - disabling it would post nothing and silently do
 * nothing. A flag guards the second submit instead.
 */
(function () {
    'use strict';

    function wire(form) {
        if (form.dataset.busyWired) { return; }
        form.dataset.busyWired = '1';

        form.addEventListener('submit', function (event) {
            if (form.dataset.busySubmitting) {
                // Already on its way. Swallow the second press rather than
                // starting the work twice.
                event.preventDefault();
                return;
            }

            // Let the browser's own validation reject it first, or the form
            // would sit there looking busy having never gone anywhere.
            if (typeof form.checkValidity === 'function' && !form.checkValidity()) {
                return;
            }

            form.dataset.busySubmitting = '1';

            var button = form.querySelector('button[type="submit"], button:not([type])');
            if (!button) { return; }

            button.classList.add('is-busy');
            button.setAttribute('aria-busy', 'true');

            var label = button.querySelector('.btn-label');
            var busyLabel = button.dataset.busyLabel;
            // The words carry the message on their own, which is what a
            // reader who cannot see a spinning ring - or has asked for no
            // animation - is left with.
            if (label && busyLabel) { label.textContent = busyLabel; }
        });
    }

    function init() {
        document.querySelectorAll('form[data-busy]').forEach(wire);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
