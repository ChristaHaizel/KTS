import random
from django.db import transaction
from .models import Course, Room, TimeSlot, StudentGroup, TimetableEntry

def run_genetic_algorithm():
    courses = list(Course.objects.all().select_related('lecturer'))
    rooms = list(Room.objects.all())
    timeslots = list(TimeSlot.objects.all())
    groups = list(StudentGroup.objects.all())

    if not courses or not rooms or not timeslots or not groups:
        return {'success': False, 'message': 'Please add Courses, Rooms, Time Slots and Student Groups first.'}

    def create_individual():
        individual = []
        used_slots = set()
        for i, course in enumerate(courses):
            suitable_rooms = [r for r in rooms if r.capacity >= course.expected_students] or rooms
            group = groups[i % len(groups)]
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

    POPULATION_SIZE = 50
    GENERATIONS = 300
    population = [create_individual() for _ in range(POPULATION_SIZE)]

    best = None
    best_fitness = 0

    for generation in range(GENERATIONS):
        scored = sorted(population, key=fitness, reverse=True)
        top_fitness = fitness(scored[0])
        if top_fitness > best_fitness:
            best_fitness = top_fitness
            best = scored[0]
        if top_fitness == 1.0:
            break
        survivors = scored[:POPULATION_SIZE // 2]
        children = []
        while len(children) < POPULATION_SIZE // 2:
            p1, p2 = random.sample(survivors, 2)
            child = crossover(p1, p2)
            child = mutate(child)
            children.append(child)
        population = survivors + children

    if best is None:
        return {'success': False, 'message': 'Algorithm failed to produce a result.'}

    # Deduplicate before saving — no two entries share the same room+timeslot
    seen = set()
    clean_best = []
    for gene in best:
        key = (gene['timeslot'].id, gene['room'].id)
        if key not in seen:
            seen.add(key)
            clean_best.append(gene)

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
        return {'success': True, 'entries_created': created}
    except Exception as e:
        return {'success': False, 'message': str(e)}