from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="LaneOverride",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("merge", "merge"), ("split", "split")],
                                          max_length=8)),
                ("group_label", models.CharField(
                    help_text="The heuristic lane (job name + numeric-normalized args) "
                              "this override applies to.",
                    max_length=500, unique=True)),
                ("merge_name", models.CharField(
                    blank=True, help_text="Merged lane name (merge overrides only).",
                    max_length=255)),
            ],
            options={"verbose_name": "lane override"},
        ),
    ]
