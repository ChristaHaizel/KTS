import random
from django.db import transaction
from .models import Room, TimeSlot, StudentGroup, TimetableEntry

POPULATION_SIZE = 30
GENERATIONS = 120
STAGNATION_LIMIT = 25

def run_genetic_algorithm():
    # A gene is one (group, course) enrollment pair, not one course. A course taken by
    # two groups needs two scheduled classes; a group must never be given a course it
    # is not enrolled in.
    enrollments = [
        (group, course)
        for group in StudentGroup.objects.prefetch_related('courses__lecturer')
        for course in group.courses.all()
    ]
    rooms = list(Room.objects.all())
    timeslots = list(TimeSlot.objects.all())

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
            suitable_rooms = [r for r in rooms if r.capacity >= course.expected_students] or rooms
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

    def fitness(individual):
        penalties = 0
        seen_room_slots = {}
        seen_lecturer_slots = {}
        seen_group_slots = {}
        for gene in individual:
            rs_key = (gene['timeslot'].id, gene['room'].id)
            if rs_key in seen_room_slots:
                penalties += 10
            seen_room_slots[rs_key] = True

            if gene['course'].lecturer:
                lec_key = (gene['timeslot'].id, gene['course'].lecturer.id)
                if lec_key in seen_lecturer_slots:
                    penalties += 10
                seen_lecturer_slots[lec_key] = True

            grp_key = (gene['timeslot'].id, gene['group'].id)
            if grp_key in seen_group_slots:
                penalties += 5
            seen_group_slots[grp_key] = True

            if gene['room'].capacity < gene['course'].expected_students:
                penalties += 2

        return 1 / (1 + penalties)

    def crossover(parent1, parent2):
        point = random.randint(1, len(parent1) - 1)
        return parent1[:point] + parent2[point:]

    def mutate(individual, mutation_rate=0.15):
        used_slots = {(g['timeslot'].id, g['room'].id) for g in individual}
        for gene in individual:
            if random.random() < mutation_rate:
                suitable_rooms = [r for r in rooms if r.capacity >= gene['course'].expected_students] or rooms
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

    population = [create_individual() for _ in range(POPULATION_SIZE)]

    best = None
    best_fitness = 0
    stagnant = 0

    for generation in range(GENERATIONS):
        # Score once per individual per generation. Calling fitness() as sorted()'s key
        # re-evaluates the whole population every comparison, which is what made this
        # slow enough to blow the HTTP timeout on real data.
        scored = sorted(
            ((fitness(ind), ind) for ind in population),
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
            'message': (f'{dropped} class(es) could not be placed — add more rooms '
                        f'or time slots.') if dropped else '',
        }
    except Exception as e:
        return {'success': False, 'message': str(e)}
