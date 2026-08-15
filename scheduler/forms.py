from django import forms

from .models import (
    College, Course, Department, Lecturer, Room, Student, StudentGroup, TimeSlot,
)


class DepartmentSelect(forms.Select):
    """A department dropdown whose options say which college they belong to.

    The browser needs that to narrow the list once a college is picked; the
    option text is only the department's name, which is not enough to filter
    on. Rendered as data-college so the script has something to read.
    """

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        college_id = getattr(getattr(value, 'instance', None), 'college_id', None)
        if college_id is not None:
            option['attrs']['data-college'] = str(college_id)
        return option


class StudentActivationForm(forms.Form):
    """A student claiming the record the timetable office already holds.

    This creates no student. It finds one, and the finding is the security: an
    account is only ever attached to a row that was on the roster first.

    Both the ID and the index number have to point at the same row. Student IDs
    run in sequence, so one can be guessed from a classmate's, and this ends
    with a password being emailed to whatever address was typed in - which
    would make guessing an ID a way of taking over that student's account. The
    index number is not derivable from the ID and is already on file for
    everyone imported.
    """

    college = forms.ModelChoiceField(
        queryset=College.objects.all(),
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_college'}),
    )
    department = forms.ModelChoiceField(
        queryset=Department.objects.select_related('college'),
        widget=DepartmentSelect(attrs={'class': 'form-select', 'id': 'id_department'}),
        help_text='Your programme.',
    )
    student_id = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control',
                                      'placeholder': 'e.g. 20212007'}),
    )
    index_number = forms.CharField(
        max_length=20,
        label='Index number',
        widget=forms.TextInput(attrs={'class': 'form-control',
                                      'placeholder': 'e.g. 7212007'}),
        help_text='The number on your exam scripts. It proves the record is yours.',
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control',
                                       'placeholder': 'you@st.knust.edu.gh'}),
        help_text='Your password is sent here, so use one you can open.',
    )

    def clean_student_id(self):
        return self.cleaned_data['student_id'].strip()

    def clean_index_number(self):
        return self.cleaned_data['index_number'].strip()

    def clean(self):
        cleaned = super().clean()
        college = cleaned.get('college')
        department = cleaned.get('department')
        student_id = cleaned.get('student_id')
        index_number = cleaned.get('index_number')

        if college and department and department.college_id != college.pk:
            self.add_error('department',
                           'That department is not in that college.')

        if not (student_id and index_number):
            return cleaned

        student = Student.objects.filter(student_id=student_id).first()

        # One message for "no such ID" and for "that is not your index number".
        # Telling them apart would turn this form into a way of discovering
        # which student IDs exist, one guess at a time.
        mismatch = (
            'We could not match those details to a student record. Check your '
            'student ID and index number, and ask the timetable office if they '
            'still do not work.'
        )
        if student is None or (student.index_number or '') != index_number:
            raise forms.ValidationError(mismatch)

        if student.user_id is not None:
            raise forms.ValidationError(
                'That student already has an account. Sign in instead, or use '
                'the forgotten password link if you cannot get in.'
            )

        cleaned['student'] = student
        return cleaned


class CollegeForm(forms.ModelForm):
    class Meta:
        model = College
        fields = ['name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control',
                                           'placeholder': 'e.g. College of Science'}),
        }


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ['name', 'college']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control',
                                           'placeholder': 'e.g. Computer Science'}),
            'college': forms.Select(attrs={'class': 'form-select'}),
        }
        help_texts = {
            'name': 'This is what students see as their programme.',
        }


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
        fields = ['name', 'size', 'courses']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. CS Level 400 Group 1'}),
            'size': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'placeholder': 'e.g. 45'}),
            'courses': forms.CheckboxSelectMultiple,
        }
        labels = {'size': 'Number of students'}
        help_texts = {
            'size': (
                'How many sit in this group. This is what rooms are matched '
                'against, so a cohort split in two is sized by the half that '
                'actually attends.'
            ),
        }


class MyEmailForm(forms.Form):
    """The address a user keeps for themselves.

    It has to land in two places: on the account, because that is where the
    password reset looks, and on the lecturer or student record, because that
    is what an administrator sees. Letting them drift apart means a reset going
    somewhere the office cannot see.
    """
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. aserwaa@st.knust.edu.gh',
        }),
        help_text='Leave blank to remove it. Without one you cannot reset your '
                  'own password.',
    )

    def __init__(self, *args, user=None, profile=None, **kwargs):
        self.user = user
        self.profile = profile
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            return ''
        # A lecturer's address is unique, so refuse here with a readable message
        # rather than letting the database raise on save.
        from .models import Lecturer
        clash = Lecturer.objects.filter(email__iexact=email)
        if self.profile is not None:
            clash = clash.exclude(pk=self.profile.pk)
        if isinstance(self.profile, Lecturer) or clash.exists():
            if clash.exists():
                raise forms.ValidationError(
                    'Another lecturer already uses that address.'
                )
        return email

    def save(self):
        email = self.cleaned_data['email']
        self.user.email = email
        self.user.save(update_fields=['email'])
        if self.profile is not None and hasattr(self.profile, 'email'):
            self.profile.email = email
            self.profile.save(update_fields=['email'])
        return email


class StudentForm(forms.ModelForm):
    """Programme is not on this form: a department is what sets it.

    Leaving both would let them disagree, and the one that survives on the
    student's own account is the department. A student whose programme was
    imported as text and matches no department keeps that text - the field is
    still there, just not something to type into twice.
    """

    class Meta:
        model = Student
        fields = ['student_id', 'index_number', 'name', 'email',
                  'college', 'department', 'level', 'group']
        widgets = {
            'student_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 20512345'}),
            'index_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 7212345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Ama Serwaa'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'e.g. aserwaa@st.knust.edu.gh'}),
            'college': forms.Select(attrs={'class': 'form-select', 'id': 'id_college'}),
            'department': DepartmentSelect(attrs={'class': 'form-select', 'id': 'id_department'}),
            'level': forms.Select(attrs={'class': 'form-select'}),
            'group': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'student_id': 'Student ID',
            'index_number': 'Index number',
            'group': 'Student group',
        }
        help_texts = {
            'student_id': 'This is what they sign in with.',
            'index_number': 'Optional, but a student needs it to set up their '
                            'own account.',
            'email': 'Needed only so they can reset their own password.',
            'department': 'This is their programme.',
            'group': 'The teaching group whose timetable is theirs.',
        }

    def clean(self):
        cleaned = super().clean()
        college = cleaned.get('college')
        department = cleaned.get('department')
        if college and department and department.college_id != college.pk:
            self.add_error('department', 'That department is not in that college.')
        return cleaned


class TimeSlotForm(forms.ModelForm):
    class Meta:
        model = TimeSlot
        fields = ['day', 'start_time', 'end_time']
        widgets = {
            'day': forms.Select(attrs={'class': 'form-select'}),
            'start_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
        }
