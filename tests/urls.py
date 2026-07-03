from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("scheduler/", include("scheduler_monitor.urls")),
]
