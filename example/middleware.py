"""Example-project only: keep a demo staff user logged in on every request.

The packaged views are (correctly) ``staff_member_required``. In the real
product an expired session redirects to the admin login — that's desired. But
for the throwaway demo we don't want reloading ``/scheduler/`` (or any deep
link) to bounce to a login page, so this middleware transparently signs in a
demo staff user when the request is anonymous. It is wired only in this example
settings file and never ships with the package.
"""

from django.contrib.auth import get_user_model, login


class DemoAutoLoginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            User = get_user_model()
            user, created = User.objects.get_or_create(
                username="demo",
                defaults={"is_staff": True, "is_superuser": True})
            if created:
                user.set_password("demo")
                user.save()
            login(request, user)
        return self.get_response(request)
