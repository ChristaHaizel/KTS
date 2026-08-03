from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.baselines import compare
from scheduler.genetic_algorithm import run_genetic_algorithm
from scheduler.models import Room, TimeSlot


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

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                self._benchmark(options)
                raise _Rollback()
        except _Rollback:
            self.stdout.write('\nAll benchmark writes rolled back.')

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
