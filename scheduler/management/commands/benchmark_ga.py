from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.baselines import compare
from scheduler.genetic_algorithm import run_genetic_algorithm
from scheduler.models import Room, TimeSlot


MIN_SENSITIVITY_TRIALS = 20


class _Rollback(Exception):
    """Raised to undo everything a benchmark touched."""


class Command(BaseCommand):
    help = (
        'Compare the genetic algorithm against random and greedy baselines on the '
        'current dataset, using the same fitness function for all three. Every '
        'write is rolled back, so the live timetable is never replaced.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--trials', type=int, default=5,
            help='How many times to run each approach (default 5).',
        )
        parser.add_argument(
            '--rooms', type=int, default=None,
            help='Benchmark against only this many rooms, to tighten the problem.',
        )
        parser.add_argument(
            '--slots', type=int, default=None,
            help='Benchmark against only this many time slots.',
        )
        parser.add_argument(
            '--sensitivity', action='store_true',
            help=(
                'Re-run under several penalty weightings and report the violations '
                'each produces, to show whether the result depends on the weights.'
            ),
        )

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                self._benchmark(options)
                raise _Rollback()
        except _Rollback:
            self.stdout.write('\nAll benchmark writes rolled back.')

    def _sensitivity(self, trials):
        """Does the outcome actually depend on the penalty weights?

        Violations are counted rather than fitness compared: fitness is defined
        by the weights, so scores from different weightings are not comparable.
        """
        weightings = [
            ('default 10/10/5/2', {'room': 10, 'lecturer': 10, 'group': 5, 'capacity': 2}),
            ('flat 1/1/1/1', {'room': 1, 'lecturer': 1, 'group': 1, 'capacity': 1}),
            ('hard only 10/10/10/0', {'room': 10, 'lecturer': 10, 'group': 10, 'capacity': 0}),
            ('capacity-led 1/1/1/10', {'room': 1, 'lecturer': 1, 'group': 1, 'capacity': 10}),
            ('extreme 100/100/50/1', {'room': 100, 'lecturer': 100, 'group': 50, 'capacity': 1}),
        ]

        # The search is stochastic and the differences here are small, so a
        # handful of runs produces orderings that reverse on the next attempt.
        if trials < MIN_SENSITIVITY_TRIALS:
            self.stdout.write(self.style.WARNING(
                f'{trials} trials is too few to separate these weightings: run-to-run '
                f'variance is larger than the differences between them, and an '
                f'apparent winner here will not survive a rerun. Use --trials '
                f'{MIN_SENSITIVITY_TRIALS} or more before believing any of it.\n'
            ))

        self.stdout.write('Mean violations over '
                          f'{trials} runs, by penalty weighting:\n')
        self.stdout.write(
            f"{'Weighting':<24}{'room':>7}{'lect':>7}{'group':>7}{'cap':>7}{'total':>8}"
        )
        self.stdout.write('-' * 60)

        totals = []
        for label, weights in weightings:
            runs = [run_genetic_algorithm(weights=weights) for _ in range(trials)]
            runs = [r for r in runs if r['success']]
            if not runs:
                self.stdout.write(f'{label:<24}  (no successful run)')
                continue
            mean = {
                key: sum(r['violations'][key] for r in runs) / len(runs)
                for key in ('room', 'lecturer', 'group', 'capacity')
            }
            total = sum(mean.values())
            totals.append(total)
            self.stdout.write(
                f"{label:<24}{mean['room']:>7.1f}{mean['lecturer']:>7.1f}"
                f"{mean['group']:>7.1f}{mean['capacity']:>7.1f}{total:>8.1f}"
            )

        if len(totals) > 1:
            spread = max(totals) - min(totals)
            self.stdout.write('')
            if trials < MIN_SENSITIVITY_TRIALS:
                self.stdout.write(self.style.WARNING(
                    f'Spread {spread:.1f}, but with only {trials} trials that is '
                    'within noise. No conclusion either way.'
                ))
            elif spread < 0.5:
                self.stdout.write(self.style.WARNING(
                    f'Total violations vary by only {spread:.1f} across every '
                    'weighting. On this dataset the weights make no practical '
                    'difference - the search finds the same quality of timetable '
                    'whatever they are, so the exact numbers are not load-bearing.'
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f'Total violations vary by {spread:.1f} across weightings, so '
                    'the choice of weights does change the timetable produced.'
                ))

    def _benchmark(self, options):
        trials = options['trials']

        # Trimming happens inside the rolled-back transaction, so a tighter
        # problem can be explored without touching the real dataset.
        if options['rooms'] is not None:
            keep = list(Room.objects.values_list('pk', flat=True)[:options['rooms']])
            Room.objects.exclude(pk__in=keep).delete()
        if options['slots'] is not None:
            keep = list(TimeSlot.objects.values_list('pk', flat=True)[:options['slots']])
            TimeSlot.objects.exclude(pk__in=keep).delete()

        if options['sensitivity']:
            self._sensitivity(trials)
            return

        baseline = compare(trials=trials)
        if baseline is None:
            self.stdout.write(self.style.ERROR(
                'Nothing to schedule. Add rooms, time slots, and at least one '
                'student group with courses before benchmarking.'
            ))
            return

        self.stdout.write(
            f"Problem: {baseline['classes']} classes, {baseline['rooms']} rooms, "
            f"{baseline['timeslots']} time slots, {trials} trials each.\n"
        )

        ga_scores, ga_times, ga_generations = [], [], []
        for _ in range(trials):
            result = run_genetic_algorithm()
            if not result['success']:
                self.stdout.write(self.style.ERROR(result['message']))
                return
            ga_scores.append(result['fitness'])
            ga_times.append(result['runtime_seconds'])
            ga_generations.append(result['generations_run'])

        ga_mean = sum(ga_scores) / len(ga_scores)
        rows = [
            ('Random', baseline['random_mean'], baseline['random_best'], None),
            ('Greedy first-fit', baseline['greedy_mean'], baseline['greedy_best'], None),
            ('Genetic algorithm', ga_mean, max(ga_scores),
             sum(ga_times) / len(ga_times)),
        ]

        self.stdout.write(f"{'Approach':<20}{'mean':>10}{'best':>10}{'mean time':>12}")
        self.stdout.write('-' * 52)
        for name, mean, best, seconds in rows:
            timing = f'{seconds:.3f}s' if seconds is not None else '-'
            self.stdout.write(f'{name:<20}{mean:>10.4f}{best:>10.4f}{timing:>12}')

        self.stdout.write(
            f"\nGA converged in {sum(ga_generations) / len(ga_generations):.1f} "
            f"generations on average."
        )

        greedy = baseline['greedy_mean']
        if ga_mean > greedy:
            self.stdout.write(self.style.SUCCESS(
                f'GA beats greedy by {(ga_mean - greedy):.4f} mean fitness.'
            ))
        elif ga_mean == greedy:
            self.stdout.write(self.style.WARNING(
                'GA only matches greedy here. Both score perfectly, so this problem '
                'is too loosely constrained to tell them apart. Re-run with '
                '--rooms/--slots to squeeze it until they separate.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'Greedy beats the GA by {(greedy - ga_mean):.4f} mean fitness here.'
            ))
