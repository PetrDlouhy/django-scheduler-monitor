from django.contrib import admin

from .models import LaneOverride


@admin.register(LaneOverride)
class LaneOverrideAdmin(admin.ModelAdmin):
    list_display = ("kind", "group_label", "merge_name")
    list_filter = ("kind",)
    search_fields = ("group_label", "merge_name")
