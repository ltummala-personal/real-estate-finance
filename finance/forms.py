import hashlib
from pathlib import Path

from django import forms
from django.conf import settings
from django.db.models import Q

from .models import Account, Category, Direction, ExtractedEntry, Project, SourceDocument, SourceKind, Transaction

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
ALLOWED_EXTENSIONS = {
    SourceKind.BILL.value: {".pdf"} | IMAGE_EXTENSIONS,
    SourceKind.INVOICE.value: {".pdf"} | IMAGE_EXTENSIONS,
    SourceKind.BANK_STATEMENT.value: {".csv", ".xlsx", ".pdf"},
    SourceKind.CSV.value: {".csv", ".xlsx"},
}


def content_matches_extension(suffix, data):
    head = data[:1024]
    if suffix == ".pdf":
        return b"%PDF-" in head
    if suffix == ".xlsx":
        return head.startswith(b"PK\x03\x04")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix in {".tif", ".tiff"}:
        return head.startswith((b"II*\x00", b"MM\x00*"))
    if suffix == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".csv":
        return b"\x00" not in data[:8192]
    return False


class DocumentUploadForm(forms.ModelForm):
    class Meta:
        model = SourceDocument
        fields = ["kind", "file", "account", "project"]

    def clean(self):
        cleaned = super().clean()
        upload, kind = cleaned.get("file"), cleaned.get("kind")
        if not upload or not kind:
            return cleaned

        suffix = Path(upload.name).suffix.lower()
        allowed = ALLOWED_EXTENSIONS[str(kind)]
        if suffix not in allowed:
            self.add_error("file", f"Allowed file types for this document: {', '.join(sorted(allowed))}.")
            return cleaned
        if upload.size > settings.DOCUMENT_MAX_UPLOAD_BYTES:
            limit_mb = settings.DOCUMENT_MAX_UPLOAD_BYTES // (1024 * 1024)
            self.add_error("file", f"File is too large (limit {limit_mb} MB).")
            return cleaned

        data = upload.read()
        upload.seek(0)
        if not content_matches_extension(suffix, data):
            self.add_error("file", "The file content does not match its extension.")
            return cleaned
        digest = hashlib.sha256(data).hexdigest()
        if SourceDocument.objects.filter(sha256=digest).exists():
            self.add_error("file", "This exact file has already been uploaded.")
            return cleaned
        if kind == SourceKind.BANK_STATEMENT and not cleaned.get("account"):
            self.add_error("account", "Choose the account this statement belongs to.")

        self.file_bytes = data
        self.sha256 = digest
        return cleaned


class EntryReviewForm(forms.ModelForm):
    DECISIONS = [("approve", "Approve"), ("later", "Decide later"), ("reject", "Reject")]
    decision = forms.ChoiceField(choices=DECISIONS)

    class Meta:
        model = ExtractedEntry
        fields = ["date", "direction", "amount", "description", "counterparty", "reference", "category", "project"]
        widgets = {"date": DATE_WIDGET}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        entry = self.instance
        complete = entry.date and entry.amount and entry.description
        self.fields["decision"].initial = "approve" if complete and not entry.possible_duplicate else "later"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == "approve":
            for name in ("date", "amount", "description"):
                if not cleaned.get(name) and name not in self.errors:
                    self.add_error(name, "Required to approve.")
            amount = cleaned.get("amount")
            if amount is not None and amount <= 0:
                self.add_error("amount", "Amount must be greater than zero.")
        return cleaned


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = [
            "date", "direction", "amount", "description", "counterparty", "reference",
            "project", "account", "category",
        ]
        widgets = {"date": DATE_WIDGET}


class TransactionFilterForm(forms.Form):
    date_from = forms.DateField(required=False, widget=DATE_WIDGET, label="From")
    date_to = forms.DateField(required=False, widget=DATE_WIDGET, label="To")
    project = forms.ModelChoiceField(Project.objects.all(), required=False)
    account = forms.ModelChoiceField(Account.objects.all(), required=False)
    category = forms.ModelChoiceField(Category.objects.all(), required=False)
    direction = forms.ChoiceField(choices=[("", "All")] + Direction.choices, required=False)
    source = forms.ChoiceField(choices=[("", "All")] + SourceKind.choices, required=False)
    q = forms.CharField(required=False, max_length=100, label="Search")

    def filter(self, queryset):
        if not self.is_valid():
            return queryset
        data = self.cleaned_data
        if data["date_from"]:
            queryset = queryset.filter(date__gte=data["date_from"])
        if data["date_to"]:
            queryset = queryset.filter(date__lte=data["date_to"])
        for name in ("project", "account", "category", "direction", "source"):
            if data[name]:
                queryset = queryset.filter(**{name: data[name]})
        if data["q"]:
            queryset = queryset.filter(
                Q(description__icontains=data["q"])
                | Q(counterparty__icontains=data["q"])
                | Q(reference__icontains=data["q"])
            )
        return queryset
