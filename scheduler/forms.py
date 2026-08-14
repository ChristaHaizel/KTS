import smtplib

from django import forms

from .models import Lecturer, Course, Room, Student, StudentGroup, TimeSlot

# What a mail server that will not take the message looks like from in here.
# OSError covers a refused connection, a host that does not resolve, a timeout
# and a TLS failure; SMTPException covers everything the protocol itself can
# object to, of which rejected credentials is by far the most likely.
#
# Django's own PasswordResetForm.send_mail already catches these and logs them,
# so a reset does not fail on a mail server that is down. This is here for the
# test-send on My Account, which needs to report the reason rather than bury it.
MAIL_DELIVERY_ERRORS = (OSError, smtplib.SMTPException)


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
    class Meta:
        model = Student
        fields = ['student_id', 'index_number', 'name', 'email', 'programme', 'level', 'group']
        widgets = {
            'student_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 20512345'}),
            'index_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. 7212345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Ama Serwaa'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'e.g. aserwaa@st.knust.edu.gh'}),
            'programme': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BSc Computer Science'}),
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
            'index_number': 'Optional. Leave blank if they only have one number.',
            'email': 'Needed only so they can reset their own password.',
            'group': 'The teaching group whose timetable is theirs.',
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
