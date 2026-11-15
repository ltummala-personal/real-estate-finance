import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=30, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("location", models.CharField(blank=True, max_length=200)),
                (
                    "budget",
                    models.DecimalField(
                        decimal_places=2,
                        default=0,
                        max_digits=14,
                        validators=[django.core.validators.MinValueValidator(0)],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["name"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("budget__gte", 0)),
                        name="project_budget_nonnegative",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.CharField(max_length=250)),
                ("vendor", models.CharField(blank=True, max_length=200)),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=14,
                        validators=[django.core.validators.MinValueValidator(0.01)],
                    ),
                ),
                ("incurred_on", models.DateField()),
                ("reference", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="expenses",
                        to="finance.project",
                    ),
                ),
            ],
            options={
                "ordering": ["-incurred_on", "-created_at"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="expense_amount_positive",
                    ),
                ],
            },
        ),
    ]
