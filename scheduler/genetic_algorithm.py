import random
import time
from django.db import transaction
from django.db.models import Count
from .models import Room, TimeSlot, StudentGroup, TimetableEntry

POPULATION_SIZE = 30
GENERATIONS = 120
STAGNATION_LIMIT = 25

# Penalty weights. A hard clash - two classes in one room, or one lecturer in two
# places - makes a timetable unusable, so those carry the heaviest penalty. A group
# clash is equally impossible in reality but is weighted lower because a group can
# be split across sessions in a way a room or a person cannot. Capacity is the only
# soft constraint: an over-full room is workable, merely bad, so it costs least.
PENALTY_ROOM_CLASH = 10
PENALTY_LECTURER_CLASH = 10
PENALTY_GROUP_CLASH = 5
PENALTY_OVER_CAPACITY = 2

DEFAULT_WEIGHTS = {
    'room': PENALTY_ROOM_CLASH,
    'lecturer': PENALTY_LECTURER_CLASH,
    'group': PENALTY_GROUP_CLASH,
    'capacity': PENALTY_OVER_CAPACITY,
}


def count_violations(individual):
    """How many of each constraint a candidate breaks.

    Weight-independent, so results from differently weighted runs can be
    compared. Fitness cannot: it is defined by the weights, so a lower score
    under one weighting says nothing about a score under another.
    """
    counts = {'room': 0, 'lecturer': 0, 'group': 0, 'capacity': 0}
    seen_room_slots = set()
    seen_lecturer_slots = set()
    seen_group_slots = set()

    for gene in individual:
        rs_key = (gene['timeslot'].id, gene['room'].id)
        if rs_key in seen_room_slots:
            counts['room'] += 1
        seen_room_slots.add(rs_key)

        if gene['course'].lecturer:
            lec_key = (gene['timeslot'].id, gene['course'].lecturer.id)
            if lec_key in seen_lecturer_slots:
                counts['lecturer'] += 1
            seen_lecturer_slots.add(lec_key)

        grp_key = (gene['timeslot'].id, gene['group'].id)
        if grp_key in seen_group_slots:
            counts['group'] += 1
        seen_group_slots.add(grp_key)

        # Sized against the group sitting the class, not the course's total.
        # A cohort is split precisely so the parts fit rooms the whole does
        # not; measuring against the whole would report every split class as
        # over capacity and push the search towards halls nobody needs.
        if gene['room'].capacity < gene['group'].size_for(gene['course']):
            counts['capacity'] += 1

    return counts


def fitness(individual, weights=None):
    """Score a candidate timetable in (0, 1]. 1.0 means no penalties at all."""
    w = weights or DEFAULT_WEIGHTS
    counts = count_violations(individual)
    penalties = (
        counts['room'] * w['room']
        + counts['lecturer'] * w['lecturer']
        + counts['group'] * w['group']
        + counts['capacity'] * w['capacity']
    )
    return 1 / (1 + penalties)


def load_problem():
    """The scheduling problem as the algorithm and its baselines both see it.

    A gene is one (group, course) enrollment pair, not one course. A course taken
    by two groups needs two scheduled classes; a group must never be given a
    course it is not enrolled in.
    """
    # Annotated so size_for() never runs a count per gene per generation.
    groups = (StudentGroup.objects
              .prefetch_related('courses__lecturer')
              .annotate(enrolled_count=Count('students')))
    enrollments = [
        (group, course)
        for group in groups
        for course in group.courses.all()
    ]
    return (
        enrollments,
        list(Room.objects.all()),
        list(TimeSlot.objects.all()),
    )


def run_genetic_algorithm(weights=None):
    enrollments, rooms, timeslots = load_problem()

    if not enrollments or not rooms or not timeslots:
        return {
            'success': False,
            'message': 'Add Rooms, Time Slots, and at least one Student Group '
                       'with courses assigned before generating.',
        }

    def create_individual():
        individual = []
        used_slots = set()
        for group, course in enrollments:
            needed = group.size_for(course)
            suitable_rooms = [r for r in rooms if r.capacity >= needed] or rooms
            attempts = 0
            while attempts < 100:
                room = random.choice(suitable_rooms)
                timeslot = random.choice(timeslots)
                key = (timeslot.id, room.id)
                if key not in used_slots:
                    used_slots.add(key)
                    individual.append({'course': course, 'room': room, 'timeslot': timeslot, 'group': group})
                    break
                attempts += 1
            else:
                room = random.choice(suitable_rooms)
                timeslot = random.choice(timeslots)
                individual.append({'course': course, 'room': room, 'timeslot': timeslot, 'group': group})
        return individual

    def crossover(parent1, parent2):
        point = random.randint(1, len(parent1) - 1)
        return parent1[:point] + parent2[point:]

    def mutate(individual, mutation_rate=0.15):
        used_slots = {(g['timeslot'].id, g['room'].id) for g in individual}
        for gene in individual:
            if random.random() < mutation_rate:
                needed = gene['group'].size_for(gene['course'])
                suitable_rooms = [r for r in rooms if r.capacity >= needed] or rooms
                old_key = (gene['timeslot'].id, gene['room'].id)
                used_slots.discard(old_key)
                attempts = 0
                while attempts < 50:
                    new_room = random.choice(suitable_rooms)
                    new_timeslot = random.choice(timeslots)
                    new_key = (new_timeslot.id, new_room.id)
                    if new_key not in used_slots:
                        gene['room'] = new_room
                        gene['timeslot'] = new_timeslot
                        used_slots.add(new_key)
                        break
                    attempts += 1
        return individual

    started_at = time.perf_counter()
    population = [create_individual() for _ in range(POPULATION_SIZE)]

    best = None
    best_fitness = 0
    stagnant = 0
    history = []

    for generation in range(GENERATIONS):
        # Score once per individual per generation. Calling fitness() as sorted()'s key
        # re-evaluates the whole population every comparison, which is what made this
        # slow enough to blow the HTTP timeout on real data.
        scored = sorted(
            ((fitness(ind, weights), ind) for ind in population),
            key=lambda pair: pair[0],
            reverse=True,
        )
        top_fitness, top_individual = scored[0]

        if top_fitness > best_fitness:
            best_fitness = top_fitness
            best = top_individual
            stagnant = 0
        else:
            stagnant += 1

        # Best-so-far, so the curve is monotonic and reads as convergence
        # rather than as the noise of each generation's luckiest individual.
        history.append(best_fitness)

        if top_fitness == 1.0 or stagnant >= STAGNATION_LIMIT:
            break

        survivors = [ind for _, ind in scored[:POPULATION_SIZE // 2]]
        children = []
        while len(children) < POPULATION_SIZE // 2:
            p1, p2 = random.sample(survivors, 2)
            child = crossover(p1, p2)
            child = mutate(child)
            children.append(child)
        population = survivors + children

    if best is None:
        return {'success': False, 'message': 'Algorithm failed to produce a result.'}

    # Deduplicate before saving — no two active entries may share the same room+timeslot
    seen = set()
    clean_best = []
    for gene in best:
        key = (gene['timeslot'].id, gene['room'].id)
        if key not in seen:
            seen.add(key)
            clean_best.append(gene)

    dropped = len(best) - len(clean_best)

    try:
        with transaction.atomic():
            TimetableEntry.objects.all().update(is_active=False)
            created = 0
            for gene in clean_best:
                TimetableEntry.objects.create(
                    course=gene['course'],
                    room=gene['room'],
                    timeslot=gene['timeslot'],
                    student_group=gene['group'],
                    is_active=True
                )
                created += 1
        return {
            'success': True,
            'entries_created': created,
            'dropped': dropped,
            'fitness': best_fitness,
            'violations': count_violations(clean_best),
            'history': history,
            'generations_run': len(history),
            'runtime_seconds': time.perf_counter() - started_at,
            'message': (f'{dropped} class(es) could not be placed — add more rooms '
                        f'or time slots.') if dropped else '',
        }
    except Exception as e:
        return {'success': False, 'message': str(e)}
