from django import forms
from .models import Lecturer, Course, Room, Student, StudentGroup, TimeSlot


class LecturerForm(forms.ModelForm):
    class Meta:
        model = Lecturer
        fields = ['name', 'email', 'user']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Dr. Kwame Mensah'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'e.g. kmensah@knust.edu.gh'}),
            'user': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {'user': 'Login account'}
        help_texts = {
            'user': 'Optional. Linking an account lets this lecturer sign in and '
                    'raise reschedule requests for their own classes.',
        }


class CourseForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = ['code', 'name', 'expected_students', 'lecturer']
        widgets = {
            'code': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CS 401'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Distributed Systems'}),
            'expected_students': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'lecturer': forms.Select(attrs={'class': 'form-select'}),
        }


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ['name', 'capacity']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Engineering Auditorium'}),
            'capacity': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
        }


class StudentGroupForm(forms.ModelForm):
    class Meta:
        model = StudentGroup
        fields = ['name', 'courses']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CS Level 400'}),
            'courses': forms.CheckboxSelectMultiple,
        }


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ['student_id', 'name', 'group']
        widgets = {
            'student_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 20512345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Ama Serwaa'}),
            'group': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {'group': 'Programme and level'}
        help_texts = {
            'student_id': 'This is what they sign in with.',
            'group': 'Their timetable is the timetable of this group.',
        }


class TimeSlotForm(forms.ModelForm):
    class Meta:
        model = TimeSlot
        fields = ['day', 'start_time', 'end_time']
        widgets = {
            'day': forms.Select(attrs={'class': 'form-select'}),
            'start_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
        }
