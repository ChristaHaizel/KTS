from django.contrib import admin
from django.urls import path, include, reverse_lazy
from django.contrib.auth import views as auth_views

# Django's own reset views, pointed at templates that match the rest of the app.
# They are used rather than reimplemented because they already do the parts that
# are easy to get wrong: a signed, single-use, expiring token, and answering the
# same way whether or not the address is on file, so the form cannot be used to
# discover who has an account.
urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('scheduler.urls')),
    path('login/', auth_views.LoginView.as_view(template_name='scheduler/login.html'), name='login'),
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
