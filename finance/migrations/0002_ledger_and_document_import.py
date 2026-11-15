import django.db.models.deletion
import finance.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Account",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[("bank", "Bank account"), ("cash", "Cash"), ("card", "Credit card"), ("other", "Other")],
                        default="bank",
                        max_length=10,
                    ),
                ),
                ("institution", models.CharField(blank=True, max_length=100)),
                ("number_last4", models.CharField(blank=True, max_length=4, verbose_name="last 4 digits")),
                ("opening_balance", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
            ],
            options={"ordering": ["name"], "verbose_name_plural": "categories"},
        ),
        migrations.CreateModel(
            name="SourceDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("bill", "Bill / receipt"),
                            ("invoice", "Invoice"),
                            ("bank_statement", "Bank statement"),
                            ("csv", "CSV / spreadsheet ledger"),
                        ],
                        max_length=20,
                    ),
                ),
                ("file", models.FileField(upload_to=finance.models.document_upload_path)),
                ("original_name", models.CharField(max_length=255)),
                ("sha256", models.CharField(editable=False, max_length=64, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("review", "Needs review"), ("completed", "Completed"), ("failed", "Extraction failed")],
                        default="review",
                        max_length=10,
                    ),
                ),
                ("raw_text", models.TextField(blank=True)),
                ("error", models.CharField(blank=True, max_length=500)),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        help_text="Bank/cash account the money moved through.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="documents",
                        to="finance.account",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        help_text="Default project for extracted entries.",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="documents",
                        to="finance.project",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-uploaded_at"]},
        ),
        # Existing expenses become money-out transactions; rows are kept.
        migrations.RemoveConstraint(model_name="expense", name="expense_amount_positive"),
        migrations.RenameModel(old_name="Expense", new_name="Transaction"),
        migrations.RenameField(model_name="transaction", old_name="incurred_on", new_name="date"),
        migrations.RenameField(model_name="transaction", old_name="vendor", new_name="counterparty"),
        migrations.AlterField(
            model_name="transaction",
            name="counterparty",
            field=models.CharField(blank=True, max_length=200, verbose_name="vendor / payer"),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="project",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transactions",
                to="finance.project",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="direction",
            field=models.CharField(
                choices=[("debit", "Money out"), ("credit", "Money in")], default="debit", max_length=6
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transactions",
                to="finance.account",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transactions",
                to="finance.category",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "Manual entry"),
                    ("bill", "Bill / receipt"),
                    ("invoice", "Invoice"),
                    ("bank_statement", "Bank statement"),
                    ("csv", "CSV / spreadsheet ledger"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transactions",
                to="finance.sourcedocument",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="fingerprint",
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="transaction",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterModelOptions(name="transaction", options={"ordering": ["-date", "-created_at"]}),
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.CheckConstraint(condition=models.Q(("amount__gt", 0)), name="transaction_amount_positive"),
        ),
        migrations.CreateModel(
            name="ExtractedEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(blank=True, null=True)),
                (
                    "direction",
                    models.CharField(
                        choices=[("debit", "Money out"), ("credit", "Money in")], default="debit", max_length=6
                    ),
                ),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("description", models.CharField(blank=True, max_length=250)),
                ("counterparty", models.CharField(blank=True, max_length=200, verbose_name="vendor / payer")),
                ("reference", models.CharField(blank=True, max_length=100)),
                ("notes", models.CharField(blank=True, max_length=500)),
                ("possible_duplicate", models.BooleanField(default=False)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending review"), ("approved", "Approved"), ("rejected", "Rejected")],
                        default="pending",
                        max_length=10,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="finance.category",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="entries",
                        to="finance.sourcedocument",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="finance.project",
                    ),
                ),
                (
                    "transaction",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="extracted_entry",
                        to="finance.transaction",
                    ),
                ),
            ],
            options={"ordering": ["id"], "verbose_name_plural": "extracted entries"},
        ),
    ]
