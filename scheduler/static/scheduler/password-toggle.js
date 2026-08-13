/* Wires the reveal control on every password field on the page.
 *
 * Markup contract: a .password-field wrapping an <input> and a
 * <button class="password-toggle"> holding an <i>. Anything matching that is
 * picked up, so a new password field needs no new script.
 *
 * The label states what the click will do rather than what is currently true,
 * and aria-pressed carries the state for anyone not looking at the icon.
 */
(function () {
    'use strict';

    function wire(field) {
        var input = field.querySelector('input');
        var toggle = field.querySelector('.password-toggle');
        if (!input || !toggle || toggle.dataset.wired) { return; }
        toggle.dataset.wired = '1';

        var icon = toggle.querySelector('i');

        toggle.addEventListener('click', function () {
            var wasRevealed = input.type === 'text';
            input.type = wasRevealed ? 'password' : 'text';
            if (icon) {
                icon.className = wasRevealed ? 'bi bi-eye' : 'bi bi-eye-slash';
            }
            toggle.setAttribute('aria-label', wasRevealed ? 'Show password' : 'Hide password');
            toggle.setAttribute('aria-pressed', wasRevealed ? 'false' : 'true');
            input.focus();
        });
    }

    function init() {
        document.querySelectorAll('.password-field').forEach(wire);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
