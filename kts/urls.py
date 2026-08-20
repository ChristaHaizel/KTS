from django.contrib import admin
from django.urls import path, include, reverse_lazy
from django.contrib.auth import views as auth_views

from scheduler import auth_views as kts_auth

# Django's own reset views, pointed at templates that match the rest of the app.
# They are used rather than reimplemented because they already do the parts that
# are easy to get wrong: a signed, single-use, expiring token, and answering the
# same way whether or not the address is on file, so the form cannot be used to
# discover who has an account.
urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('scheduler.urls')),
    # Three doors, one per audience, so each page can be written for the people
    # arriving at it. /login/ stays as the way in for anyone who has not been
    # told which is theirs - and as the name every {% url %} and login_required
    # redirect already points at.
    path('login/', kts_auth.login_chooser, name='login'),
    path('student/login/', kts_auth.StudentLoginView.as_view(), name='student_login'),
    path('lecturer/login/', kts_auth.LecturerLoginView.as_view(), name='lecturer_login'),
    path('office/login/', kts_auth.AdminLoginView.as_view(), name='admin_login'),
    path('student/activate/', kts_auth.activate_student, name='student_activate'),
    path('lecturer/activate/', kts_auth.activate_lecturer, name='lecturer_activate'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),

    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='scheduler/password_reset.html',
            email_template_name='scheduler/password_reset_email.txt',
            subject_template_name='scheduler/password_reset_subject.txt',
            success_url=reverse_lazy('password_reset_done'),
        ),
        name='password_reset',
    ),
    path(
        'password-reset/sent/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='scheduler/password_reset_sent.html',
        ),
        name='password_reset_done',
    ),
    path(
        'password-reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='scheduler/password_reset_confirm.html',
            success_url=reverse_lazy('password_reset_complete'),
        ),
        name='password_reset_confirm',
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='scheduler/password_reset_done.html',
        ),
        name='password_reset_complete',
    ),
]
