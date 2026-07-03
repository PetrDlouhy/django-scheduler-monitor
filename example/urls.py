from django.contrib import admin
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.urls import include, path


def demo_login(request):
    """Example-project convenience: sign in as the demo staff user and open
    the dashboard (the packaged views are staff-gated)."""
    user, created = User.objects.get_or_create(
        username="demo", defaults={"is_staff": True, "is_superuser": True})
    if created:
        user.set_password("demo")
        user.save()
    login(request, user)
    return redirect("scheduler_monitor:dashboard")


urlpatterns = [
    path("", demo_login),
    path("admin/", admin.site.urls),
    path("scheduler/", include("scheduler_monitor.urls")),
]
