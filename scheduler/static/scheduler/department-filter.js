/*
 * Narrow a department dropdown to the college chosen beside it.
 *
 * Wires itself to #id_college and #id_department wherever both are present -
 * the student activation form and the admin's student form. The server checks
 * the pair regardless; this only saves scrolling past departments that cannot
 * be right, and makes it obvious that the two fields are related.
 *
 * Each option carries data-college, put there by DepartmentSelect, because the
 * option text is only the department's name.
 */
(function () {
    'use strict';

    function wire() {
        var college = document.getElementById('id_college');
        var department = document.getElementById('id_department');
        if (!college || !department || department.dataset.filterWired) { return; }
        department.dataset.filterWired = '1';

        // Every option, remembered before any is removed. Options taken out of
        // the DOM cannot be found again.
        var all = Array.prototype.map.call(department.options, function (option) {
            return option;
        });

        function filter() {
            var chosen = college.value;
            var previous = department.value;

            department.innerHTML = '';
            all.forEach(function (option) {
                // An option with no college is the empty "---------" choice,
                // which belongs in every list.
                if (!option.dataset.college || !chosen
                        || option.dataset.college === chosen) {
                    department.appendChild(option);
                }
            });

            // Keep the current choice if the new college still contains it, so
            // opening an existing student does not silently reassign them.
            var stillThere = Array.prototype.some.call(
                department.options, function (o) { return o.value === previous; });
            department.value = stillThere ? previous : '';
        }

        college.addEventListener('change', filter);
        filter();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wire);
    } else {
        wire();
    }
})();
