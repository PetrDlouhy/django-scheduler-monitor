from django.db import models


class LaneOverride(models.Model):
    """Manual lane-grouping override on top of the numeric-normalization heuristic.

    ``merge`` rows map a heuristic group (``group_label``) into a named merged
    lane; ``split`` rows make a heuristic group fall back to exact-args lanes.
    Editable in the Django admin or through the dashboard's ⚙ lanes dialog.
    """

    KIND_MERGE = "merge"
    KIND_SPLIT = "split"
    KIND_CHOICES = [(KIND_MERGE, "merge"), (KIND_SPLIT, "split")]

    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    group_label = models.CharField(
        max_length=500, unique=True,
        help_text="The heuristic lane (job name + numeric-normalized args) this override applies to.",
    )
    merge_name = models.CharField(
        max_length=255, blank=True,
        help_text="Merged lane name (merge overrides only).",
    )

    class Meta:
        verbose_name = "lane override"

    def __str__(self):
        if self.kind == self.KIND_MERGE:
            return f"{self.group_label} → {self.merge_name}"
        return f"split {self.group_label}"


def overrides_as_dict() -> dict:
    """The dashboard/API wire format: {merges: [{name, members}], splits: [...]}."""
    merges: dict[str, list] = {}
    splits = []
    for o in LaneOverride.objects.all():
        if o.kind == LaneOverride.KIND_MERGE:
            merges.setdefault(o.merge_name, []).append(o.group_label)
        else:
            splits.append(o.group_label)
    return {"merges": [{"name": n, "members": m} for n, m in merges.items()],
            "splits": splits}


def save_overrides(data: dict):
    """Replace all overrides with the posted state (the dialog edits the whole set)."""
    LaneOverride.objects.all().delete()
    rows = [LaneOverride(kind=LaneOverride.KIND_SPLIT, group_label=g)
            for g in data.get("splits", [])]
    for group in data.get("merges", []):
        rows += [LaneOverride(kind=LaneOverride.KIND_MERGE, group_label=m,
                              merge_name=group["name"])
                 for m in group.get("members", [])]
    LaneOverride.objects.bulk_create(rows)
