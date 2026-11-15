import hashlib
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Direction(models.TextChoices):
    DEBIT = "debit", "Money out"
    CREDIT = "credit", "Money in"


class SourceKind(models.TextChoices):
    MANUAL = "manual", "Manual entry"
    BILL = "bill", "Bill / receipt"
    INVOICE = "invoice", "Invoice"
    BANK_STATEMENT = "bank_statement", "Bank statement"
    CSV = "csv", "CSV / spreadsheet ledger"


def transaction_fingerprint(account_id, date, amount, direction, description, reference):
    parts = [
        str(account_id or ""),
        date.isoformat() if date else "",
        f"{Decimal(amount):.2f}" if amount is not None else "",
        direction or "",
        " ".join((description or "").lower().split()),
        (reference or "").strip().lower(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class Project(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200, blank=True)
    budget = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(budget__gte=0), name="project_budget_nonnegative"),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Account(models.Model):
    class Kind(models.TextChoices):
        BANK = "bank", "Bank account"
        CASH = "cash", "Cash"
        CARD = "card", "Credit card"
        OTHER = "other", "Other"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BANK)
    institution = models.CharField(max_length=100, blank=True)
    number_last4 = models.CharField("last 4 digits", max_length=4, blank=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


def document_upload_path(instance, filename):
    return f"documents/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


class SourceDocument(models.Model):
    class Status(models.TextChoices):
        REVIEW = "review", "Needs review"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Extraction failed"

    kind = models.CharField(
        max_length=20,
        choices=[choice for choice in SourceKind.choices if choice[0] != SourceKind.MANUAL],
    )
    file = models.FileField(upload_to=document_upload_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, unique=True, editable=False)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="documents",
        help_text="Default project for extracted entries.",
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True, related_name="documents",
        help_text="Bank/cash account the money moved through.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REVIEW)
    raw_text = models.TextField(blank=True)
    error = models.CharField(max_length=500, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, editable=False, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.original_name}"


class Transaction(models.Model):
    date = models.DateField()
    direction = models.CharField(max_length=6, choices=Direction.choices, default=Direction.DEBIT)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(0.01)],
    )
    description = models.CharField(max_length=250)
    counterparty = models.CharField("vendor / payer", max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    source = models.CharField(max_length=20, choices=SourceKind.choices, default=SourceKind.MANUAL)
    document = models.ForeignKey(
        SourceDocument, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions"
    )
    fingerprint = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, editable=False, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="transaction_amount_positive"),
        ]

    def __str__(self):
        sign = "+" if self.direction == Direction.CREDIT else "-"
        return f"{self.date} {self.description} {sign}INR {self.amount}"

    def save(self, *args, **kwargs):
        self.fingerprint = transaction_fingerprint(
            self.account_id, self.date, self.amount, self.direction, self.description, self.reference
        )
        super().save(*args, **kwargs)


class ExtractedEntry(models.Model):
    """A row pulled out of an uploaded document, waiting for a person to confirm it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE, related_name="entries")
    date = models.DateField(null=True, blank=True)
    direction = models.CharField(max_length=6, choices=Direction.choices, default=Direction.DEBIT)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=250, blank=True)
    counterparty = models.CharField("vendor / payer", max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notes = models.CharField(max_length=500, blank=True)
    possible_duplicate = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    transaction = models.OneToOneField(
        Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="extracted_entry"
    )

    class Meta:
        ordering = ["id"]
        verbose_name_plural = "extracted entries"

    def __str__(self):
        return f"{self.date} {self.description} {self.amount}"
