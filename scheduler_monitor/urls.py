from django.urls import path

from . import views

app_name = "scheduler_monitor"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/data", views.api_data, name="api_data"),
    path("api/older", views.api_older, name="api_older"),
    path("api/output", views.api_output, name="api_output"),
    path("api/merges", views.api_merges, name="api_merges"),
]
