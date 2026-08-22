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

    function describe(button, theme) {
        var goingTo = theme === 'dark' ? 'light' : 'dark';
        button.setAttribute('aria-label', 'Switch to ' + goingTo + ' mode');
        button.setAttribute('title', 'Switch to ' + goingTo + ' mode');
        // Pressed means dark is on, so a screen reader can say which state the
        // control is in rather than only what it would do next.
        button.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');

        var icon = button.querySelector('i');
        if (icon) {
            // The icon shows what you would get, which is the convention people
            // arrive with: a moon to go dark, a sun to come back.
            icon.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon-stars';
        }
    }

    function apply(theme, buttons) {
        root.setAttribute('data-theme', theme);
        buttons.forEach(function (button) { describe(button, theme); });
    }

    function init() {
        var buttons = Array.prototype.slice.call(
            document.querySelectorAll('[data-theme-toggle]'));
        if (!buttons.length) { return; }

        apply(current(), buttons);

        buttons.forEach(function (button) {
            button.addEventListener('click', function () {
                var next = current() === 'dark' ? 'light' : 'dark';
                apply(next, buttons);
                remember(next);
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
