/*
 * The sidebar folds into a drawer below the tablet breakpoint. This opens and
 * closes it. Everything about how it looks lives in the stylesheet; this file
 * only ever sets or clears one class on <body>.
 */
(function () {
    'use strict';

    var body = document.body;
    var toggle = document.getElementById('nav-toggle');
    var sidebar = document.getElementById('sidebar');
    var backdrop = document.getElementById('nav-backdrop');
    var closeButton = document.getElementById('nav-close');

    if (!toggle || !sidebar || !backdrop) { return; }

    function isOpen() {
        return body.classList.contains('nav-open');
    }

    function setOpen(open) {
        body.classList.toggle('nav-open', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }

    function open() {
        setOpen(true);
        // Put the keyboard inside the drawer that just opened, rather than
        // leaving it on the page behind.
        var first = sidebar.querySelector('.nav-link');
        if (first) { first.focus(); }
    }

    function close(returnFocus) {
        if (!isOpen()) { return; }
        setOpen(false);
        // Focus would otherwise be left on a link that is now hidden, which
        // drops it to the top of the document.
        if (returnFocus) { toggle.focus(); }
    }

    toggle.addEventListener('click', function () {
        if (isOpen()) { close(true); } else { open(); }
    });

    // The backdrop covers the whole page including the topbar, so this also
    // catches a second tap on the button that opened the drawer.
    backdrop.addEventListener('click', function () { close(false); });

    if (closeButton) {
        closeButton.addEventListener('click', function () { close(true); });
    }

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { close(true); }
    });

    // Widening past the breakpoint puts the sidebar back into the layout by
    // itself. Without this the page would stay scroll-locked behind a backdrop
    // that is no longer drawn.
    var desktop = window.matchMedia('(min-width: 992px)');
    var onBreakpoint = function (event) {
        if (event.matches) { setOpen(false); }
    };
    if (desktop.addEventListener) {
        desktop.addEventListener('change', onBreakpoint);
    } else if (desktop.addListener) {
        desktop.addListener(onBreakpoint);
    }
})();
