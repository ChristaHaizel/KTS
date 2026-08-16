/*
 * Records matching what has been typed, listed under the search box.
 *
 * Progressive enhancement: the field is a working search box on its own, and
 * this only adds a shortcut straight to a record. Pressing Enter still submits
 * the search, so nothing is lost if the request is slow or fails.
 *
 * Turned on by data-suggest-url on the input, which _search.html adds when the
 * page passes suggest_kind.
 */
(function () {
    'use strict';

    // Just enough to collapse a burst of keystrokes into one request. Any
    // longer and the list arrives after you have stopped typing rather than
    // while you are, which is the difference between a list that keeps up and
    // one you wait for.
    var SETTLE_MS = 70;

    // One character is a useful search here: it brings up the names starting
    // with that letter, which is how you look someone up when you know how
    // their name begins and not how it is spelled.
    var MIN_LENGTH = 1;

    function wire(input) {
        var url = input.dataset.suggestUrl;
        var list = document.getElementById('search-suggestions');
        if (!url || !list || input.dataset.suggestWired) { return; }
        input.dataset.suggestWired = '1';

        var timer = null;
        var active = -1;
        var items = [];
        // Rising number, so a slow reply from an earlier keystroke cannot
        // overwrite the results of a later one.
        var latest = 0;

        function close() {
            list.innerHTML = '';
            list.classList.remove('open');
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            active = -1;
            items = [];
        }

        function highlight(index) {
            items.forEach(function (item, i) {
                item.classList.toggle('active', i === index);
                item.setAttribute('aria-selected', i === index ? 'true' : 'false');
            });
            active = index;
            if (index >= 0) {
                input.setAttribute('aria-activedescendant', items[index].id);
                items[index].scrollIntoView({block: 'nearest'});
            } else {
                input.removeAttribute('aria-activedescendant');
            }
        }

        function show(results) {
            if (!results.length) {
                close();
                return;
            }

            // Rebuilt into a fragment and swapped in one go, so the list never
            // renders empty between one set of results and the next - that
            // flash is most of what makes an as-you-type list feel jumpy.
            var fragment = document.createDocumentFragment();
            items = [];

            results.forEach(function (result, i) {
                var item = document.createElement('li');
                item.className = 'search-suggestion';
                item.id = 'search-suggestion-' + i;
                item.setAttribute('role', 'option');
                item.setAttribute('aria-selected', 'false');

                var label = document.createElement('span');
                label.className = 'suggestion-label';
                // textContent, not innerHTML: these are names out of the
                // database and must never be read as markup.
                label.textContent = result.label;

                var detail = document.createElement('span');
                detail.className = 'suggestion-detail';
                detail.textContent = result.detail || '';

                item.appendChild(label);
                item.appendChild(detail);
                item.addEventListener('mousedown', function (event) {
                    // mousedown, not click: blur fires first on a click and
                    // would close the list before the click landed.
                    event.preventDefault();
                    window.location.href = result.url;
                });
                item.addEventListener('mouseenter', function () { highlight(i); });

                fragment.appendChild(item);
                items.push(item);
            });

            list.replaceChildren(fragment);
            list.classList.add('open');
            input.setAttribute('aria-expanded', 'true');
            highlight(-1);
        }

        function fetchSuggestions() {
            var term = input.value.trim();
            if (term.length < MIN_LENGTH) { close(); return; }

            var mine = ++latest;
            fetch(url + '?q=' + encodeURIComponent(term), {
                headers: {'X-Requested-With': 'XMLHttpRequest'},
                credentials: 'same-origin'
            })
                .then(function (response) {
                    return response.ok ? response.json() : {results: []};
                })
                .then(function (data) {
                    if (mine !== latest) { return; }
                    show(data.results || []);
                })
                .catch(function () {
                    // The box still works as a search box. Nothing to say.
                    close();
                });
        }

        input.addEventListener('input', function () {
            window.clearTimeout(timer);
            timer = window.setTimeout(fetchSuggestions, SETTLE_MS);
        });

        // Coming back to a box that already has something in it should show
        // what it matches, rather than needing a keystroke first.
        input.addEventListener('focus', function () {
            if (input.value.trim().length >= MIN_LENGTH && !items.length) {
                fetchSuggestions();
            }
        });

        input.addEventListener('keydown', function (event) {
            if (!items.length) { return; }

            if (event.key === 'ArrowDown') {
                event.preventDefault();
                highlight((active + 1) % items.length);
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                highlight(active <= 0 ? items.length - 1 : active - 1);
            } else if (event.key === 'Enter' && active >= 0) {
                // Only when something is highlighted. Otherwise Enter means
                // "search for what I typed", which is the older promise.
                event.preventDefault();
                items[active].dispatchEvent(new MouseEvent('mousedown'));
            } else if (event.key === 'Escape') {
                close();
            }
        });

        input.addEventListener('blur', function () {
            // After any mousedown on the list has had its turn.
            window.setTimeout(close, 100);
        });
    }

    function init() {
        document.querySelectorAll('input[data-suggest-url]').forEach(wire);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
