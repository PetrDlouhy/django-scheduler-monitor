from django.contrib import admin

from .models import LaneOverride, Run


@admin.register(LaneOverride)
class LaneOverrideAdmin(admin.ModelAdmin):
    list_display = ("kind", "group_label", "merge_name")
    list_filter = ("kind",)
    search_fields = ("group_label", "merge_name")


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    date_hierarchy = "start"
    list_display = ("dyno", "job_label", "start", "duration_s", "outcome",
                    "peak_rss_mb", "dyno_size")
    list_filter = ("outcome", "source", "dyno_size")
    search_fields = ("job_label", "command", "dyno")
    ordering = ("-start",)
