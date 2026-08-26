/*
 * Light or dark, remembered.
 *
 * Two halves. The first is inlined into the <head> of each shell rather than
 * loaded from here, because it has to run before the page paints - a
 * stylesheet arriving first would show a white page for a frame and then
 * repaint it dark, which is worse than not offering the choice. That half only
 * sets the attribute. This file wires the button.
 *
 * There are three states to honour: chosen dark, chosen light, and "whatever
 * this machine is set to". The head script resolves the third into one of the
 * first two, so the stylesheet only ever styles an explicit data-theme and the
 * dark palette is written once.
 *
 * The control is a pair - a sun and a moon - rather than one button that
 * changes as you press it. A single button can show the state you are in or
 * the state it would move you to, and whichever it shows, half the people
 * reading it assume the other.
 */
(function () {
    'use strict';

    var STORE_KEY = 'kts-theme';
    var root = document.documentElement;

    function stored() {
        try {
            return window.localStorage.getItem(STORE_KEY);
        } catch (error) {
            // Private browsing, or storage turned off. The toggle still works
            // for this page; it just will not be remembered.
            return null;
        }
    }

    function remember(theme) {
        try {
            window.localStorage.setItem(STORE_KEY, theme);
        } catch (error) {
            /* nothing to do - see above */
        }
    }

    function current() {
        return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    function mark(buttons, theme) {
        buttons.forEach(function (button) {
            var mine = button.getAttribute('data-theme-set');
            // Which one you are in, said out loud rather than left to colour.
            button.setAttribute('aria-pressed', mine === theme ? 'true' : 'false');
        });
    }

    function apply(theme, buttons) {
        root.setAttribute('data-theme', theme);
        mark(buttons, theme);
    }

    function init() {
        var buttons = Array.prototype.slice.call(
            document.querySelectorAll('[data-theme-set]'));
        if (!buttons.length) { return; }

        apply(current(), buttons);

        buttons.forEach(function (button) {
            button.addEventListener('click', function () {
                var chosen = button.getAttribute('data-theme-set');
                // Pressing the one already on is not a no-op worth guarding:
                // it settles the choice, so the machine stops deciding.
                apply(chosen === 'dark' ? 'dark' : 'light', buttons);
                remember(chosen);
            });
        });

        // Follow the machine until somebody says otherwise. Once a choice is
        // stored, changing the system theme must not overrule it.
        var system = window.matchMedia('(prefers-color-scheme: dark)');
        var onSystemChange = function (event) {
            if (stored()) { return; }
            apply(event.matches ? 'dark' : 'light', buttons);
        };
        if (system.addEventListener) {
            system.addEventListener('change', onSystemChange);
        } else if (system.addListener) {
            system.addListener(onSystemChange);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
