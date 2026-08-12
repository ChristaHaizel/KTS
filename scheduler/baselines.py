"""Naive schedulers the genetic algorithm has to beat to justify itself.

A GA is only worth its complexity if it outperforms something simple on the
same problem and the same fitness function. These two are the comparison.
"""
import random

from django.core.cache import cache
from django.db.models import Count

from .genetic_algorithm import fitness, load_problem
from .models import Room, StudentGroup, TimeSlot

CACHE_SECONDS = 15 * 60


def random_schedule(enrollments, rooms, timeslots):
    """Assign every class a room and slot uniformly at random.

    The floor. Anything that cannot beat this is not scheduling at all.
    """
    return [
        {
            'course': course,
            'group': group,
            'room': random.choice(rooms),
            'timeslot': random.choice(timeslots),
        }
        for group, course in enrollments
    ]


def greedy_schedule(enrollments, rooms, timeslots):
    """First-fit: take each class in turn and drop it in the first free
    (room, slot) that clashes with nothing already placed.

    This is the honest bar. It is what a competent person would write without
    reaching for a metaheuristic, and on loosely constrained problems it does
    very well - which is exactly why the comparison is worth publishing.
    """
    individual = []
    used_room_slots = set()
    used_lecturer_slots = set()
    used_group_slots = set()

    # Largest classes first: they have the fewest rooms that fit them, so
    # placing them while the timetable is empty avoids painting them into a
    # corner. Sized by the group attending, as everywhere else.
    ordered = sorted(enrollments, key=lambda pair: -pair[0].size_for(pair[1]))

    for group, course in ordered:
        lecturer_id = course.lecturer_id
        placed = False

        for timeslot in timeslots:
            if (timeslot.id, group.id) in used_group_slots:
                continue
            if lecturer_id and (timeslot.id, lecturer_id) in used_lecturer_slots:
                continue

            needed = group.size_for(course)
            fitting = [r for r in rooms if r.capacity >= needed] or rooms
            for room in fitting:
                if (timeslot.id, room.id) in used_room_slots:
                    continue

                used_room_slots.add((timeslot.id, room.id))
                used_group_slots.add((timeslot.id, group.id))
                if lecturer_id:
                    used_lecturer_slots.add((timeslot.id, lecturer_id))
                individual.append({
                    'course': course, 'group': group,
                    'room': room, 'timeslot': timeslot,
                })
                placed = True
                break
            if placed:
                break

        if not placed:
            # Nowhere clean left. Place it anyway so the schedule stays complete
            # and the penalty is visible in the score rather than hidden by omission.
            individual.append({
                'course': course, 'group': group,
                'room': random.choice(rooms), 'timeslot': random.choice(timeslots),
            })

    return individual


def cache_key():
    """Changes whenever the shape of the problem does.

    Enough for a baseline comparison: the numbers only mean anything relative
    to a given set of classes, rooms and slots.
    """
    return 'baselines:{}:{}:{}'.format(
        StudentGroup.objects.aggregate(n=Count('courses'))['n'] or 0,
        Room.objects.count(),
        TimeSlot.objects.count(),
    )


def compare(trials=5, use_cache=True):
    """Score the random and greedy baselines on the current problem.

    Cached because this builds 2 x trials schedules and the Algorithm page would
    otherwise pay for it on every render. Returns None when there is nothing to
    schedule, so callers can say so rather than reporting a meaningless zero.
    """
    key = cache_key()
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    enrollments, rooms, timeslots = load_problem()
    if not enrollments or not rooms or not timeslots:
        return None

    random_scores = [
        fitness(random_schedule(enrollments, rooms, timeslots)) for _ in range(trials)
    ]
    greedy_scores = [
        fitness(greedy_schedule(enrollments, rooms, timeslots)) for _ in range(trials)
    ]

    result = {
        'classes': len(enrollments),
        'rooms': len(rooms),
        'timeslots': len(timeslots),
        'trials': trials,
        'random_mean': sum(random_scores) / len(random_scores),
        'random_best': max(random_scores),
        'greedy_mean': sum(greedy_scores) / len(greedy_scores),
        'greedy_best': max(greedy_scores),
    }
    if use_cache:
        cache.set(key, result, CACHE_SECONDS)
    return result
