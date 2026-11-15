from django.db import transaction as db_transaction

from . import extraction
from .models import Category, ExtractedEntry, Project, SourceDocument, Transaction, transaction_fingerprint


def process_document(document, data):
    """Extract rows from an uploaded file and stage them as ExtractedEntry records for review."""
    try:
        result = extraction.extract(document.kind, document.original_name, data)
    except extraction.ExtractionError as exc:
        document.status = SourceDocument.Status.FAILED
        document.error = str(exc)[:500]
        document.save(update_fields=["status", "error"])
        return 0

    categories = {c.name.lower(): c for c in Category.objects.all()}
    projects = {}
    for project in Project.objects.all():
        projects[project.code.lower()] = project
        projects[project.name.lower()] = project

    entries = []
    for row in result.rows:
        notes = list(row.notes)
        category = categories.get(row.category_name.lower()) if row.category_name else None
        if row.category_name and category is None:
            notes.append(f"Category '{row.category_name}' not found.")
        project = projects.get(row.project_name.lower()) if row.project_name else None
        if row.project_name and project is None:
            notes.append(f"Project '{row.project_name}' not found.")
        entries.append(
            ExtractedEntry(
                document=document,
                date=row.date,
                direction=row.direction or extraction.DEBIT,
                amount=row.amount,
                description=row.description,
                counterparty=row.counterparty,
                reference=row.reference,
                category=category,
                project=project or document.project,
                notes=" ".join(notes)[:500],
            )
        )

    fingerprints = {
        id(entry): transaction_fingerprint(
            document.account_id, entry.date, entry.amount, entry.direction, entry.description, entry.reference
        )
        for entry in entries
    }
    existing = set(
        Transaction.objects.filter(fingerprint__in=fingerprints.values()).values_list("fingerprint", flat=True)
    )
    for entry in entries:
        entry.possible_duplicate = fingerprints[id(entry)] in existing

    with db_transaction.atomic():
        ExtractedEntry.objects.bulk_create(entries)
        document.raw_text = result.raw_text[:100_000]
        document.status = SourceDocument.Status.REVIEW
        document.error = ""
        if result.skipped:
            document.error = f"{result.skipped} row(s) without a valid date/amount were skipped."
        document.save(update_fields=["raw_text", "status", "error"])
    return len(entries)


def entry_is_postable(entry):
    return bool(entry.date and entry.amount and entry.amount > 0 and entry.description)


def post_entry(entry, user):
    """Turn a reviewed entry into a ledger transaction."""
    document = entry.document
    txn = Transaction.objects.create(
        date=entry.date,
        direction=entry.direction,
        amount=entry.amount,
        description=entry.description,
        counterparty=entry.counterparty,
        reference=entry.reference,
        project=entry.project,
        account=document.account,
        category=entry.category,
        source=document.kind,
        document=document,
        created_by=user,
    )
    entry.transaction = txn
    entry.status = ExtractedEntry.Status.APPROVED
    entry.save()
    return txn


def approve_all(document, user):
    """Post every pending, complete, non-duplicate entry. Returns (approved, left_pending)."""
    approved = left = 0
    with db_transaction.atomic():
        pending = document.entries.select_for_update().filter(status=ExtractedEntry.Status.PENDING)
        for entry in pending.select_related("document"):
            if entry.possible_duplicate or not entry_is_postable(entry):
                left += 1
                continue
            post_entry(entry, user)
            approved += 1
        refresh_document_status(document)
    return approved, left


def refresh_document_status(document):
    if document.status == SourceDocument.Status.FAILED:
        return
    has_pending = document.entries.filter(status=ExtractedEntry.Status.PENDING).exists()
    status = SourceDocument.Status.REVIEW if has_pending else SourceDocument.Status.COMPLETED
    if document.status != status:
        document.status = status
        document.save(update_fields=["status"])
