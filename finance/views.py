import csv
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.forms import modelformset_factory
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import services
from .forms import DocumentUploadForm, EntryReviewForm, TransactionFilterForm, TransactionForm
from .models import Account, Direction, ExtractedEntry, Project, SourceDocument, Transaction

ZERO = Decimal("0.00")
REVIEW_BATCH = 50
_MONEY_IN = Q(direction=Direction.CREDIT)
_MONEY_OUT = Q(direction=Direction.DEBIT)


def _totals(queryset):
    return queryset.aggregate(
        income=Sum("amount", filter=_MONEY_IN, default=ZERO),
        expense=Sum("amount", filter=_MONEY_OUT, default=ZERO),
        count=Count("id"),
    )


def _grouped(queryset, *fields):
    return (
        queryset.values(*fields)
        .annotate(
            income=Sum("amount", filter=_MONEY_IN, default=ZERO),
            expense=Sum("amount", filter=_MONEY_OUT, default=ZERO),
            count=Count("id"),
        )
        .order_by()
    )


@login_required
def dashboard(request):
    money_in = Q(transactions__direction=Direction.CREDIT)
    money_out = Q(transactions__direction=Direction.DEBIT)
    projects = Project.objects.annotate(
        total_spent=Sum("transactions__amount", filter=money_out, default=ZERO),
        total_received=Sum("transactions__amount", filter=money_in, default=ZERO),
    )
    accounts = list(
        Account.objects.annotate(
            money_in=Sum("transactions__amount", filter=money_in, default=ZERO),
            money_out=Sum("transactions__amount", filter=money_out, default=ZERO),
        )
    )
    for account in accounts:
        account.balance = account.opening_balance + account.money_in - account.money_out

    totals = _totals(Transaction.objects.all())
    total_budget = Project.objects.aggregate(total=Sum("budget", default=ZERO))["total"]
    project_spent = Transaction.objects.filter(_MONEY_OUT, project__isnull=False).aggregate(
        total=Sum("amount", default=ZERO)
    )["total"]

    context = {
        "projects": projects,
        "accounts": accounts,
        "recent_transactions": Transaction.objects.select_related("project", "category")[:10],
        "total_budget": total_budget,
        "total_spent": totals["expense"],
        "total_income": totals["income"],
        "net_cash_flow": totals["income"] - totals["expense"],
        "remaining_budget": total_budget - project_spent,
        "pending_review": ExtractedEntry.objects.filter(status=ExtractedEntry.Status.PENDING).count(),
    }
    return render(request, "finance/dashboard.html", context)


@login_required
def transaction_list(request):
    filter_form = TransactionFilterForm(request.GET or None)
    queryset = filter_form.filter(Transaction.objects.select_related("project", "account", "category"))
    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "finance/transaction_list.html",
        {"filter_form": filter_form, "page": page, "totals": _totals(queryset), "querystring": query.urlencode()},
    )


@login_required
@require_http_methods(["GET", "POST"])
def transaction_create(request):
    form = TransactionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        txn = form.save(commit=False)
        txn.created_by = request.user
        txn.save()
        messages.success(request, "Entry recorded.")
        if "add_another" in request.POST:
            return redirect("transaction_create")
        return redirect("transaction_list")
    return render(request, "finance/transaction_form.html", {"form": form})


@login_required
def document_list(request):
    documents = SourceDocument.objects.select_related("project", "account").annotate(
        pending=Count("entries", filter=Q(entries__status=ExtractedEntry.Status.PENDING)),
        approved=Count("entries", filter=Q(entries__status=ExtractedEntry.Status.APPROVED)),
    )
    page = Paginator(documents, 50).get_page(request.GET.get("page"))
    return render(request, "finance/document_list.html", {"page": page})


@login_required
@require_http_methods(["GET", "POST"])
def document_upload(request):
    form = DocumentUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        document = form.save(commit=False)
        document.original_name = form.cleaned_data["file"].name[:255]
        document.sha256 = form.sha256
        document.uploaded_by = request.user
        document.save()
        count = services.process_document(document, form.file_bytes)
        if document.status == SourceDocument.Status.FAILED:
            messages.error(request, f"Could not extract data: {document.error}")
        else:
            messages.success(request, f"Extracted {count} entr{'y' if count == 1 else 'ies'}. Review them below.")
        return redirect("document_review", pk=document.pk)
    return render(request, "finance/document_upload.html", {"form": form})


EntryFormSet = modelformset_factory(ExtractedEntry, form=EntryReviewForm, extra=0)


@login_required
@require_http_methods(["GET", "POST"])
def document_review(request, pk):
    document = get_object_or_404(SourceDocument.objects.select_related("project", "account"), pk=pk)
    pending = document.entries.filter(status=ExtractedEntry.Status.PENDING)
    batch_ids = list(pending.values_list("id", flat=True)[:REVIEW_BATCH])
    batch = ExtractedEntry.objects.filter(id__in=batch_ids).select_related("document")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve_all":
            approved, left = services.approve_all(document, request.user)
            messages.success(request, f"Approved {approved} entries; {left} still need attention.")
            return redirect("document_review", pk=pk)
        if action == "reject_all":
            rejected = pending.update(status=ExtractedEntry.Status.REJECTED)
            services.refresh_document_status(document)
            messages.success(request, f"Rejected {rejected} entries.")
            return redirect("document_review", pk=pk)

        formset = EntryFormSet(request.POST, queryset=batch)
        if formset.is_valid():
            counts = {"approve": 0, "reject": 0, "later": 0}
            with db_transaction.atomic():
                for form in formset.forms:
                    decision = form.cleaned_data["decision"]
                    entry = form.save(commit=False)
                    if decision == "reject":
                        entry.status = ExtractedEntry.Status.REJECTED
                    entry.save()
                    if decision == "approve":
                        services.post_entry(entry, request.user)
                    counts[decision] += 1
                services.refresh_document_status(document)
            messages.success(
                request,
                f"Approved {counts['approve']}, rejected {counts['reject']}, kept {counts['later']} for later.",
            )
            return redirect("document_review", pk=pk)
    else:
        formset = EntryFormSet(queryset=batch)

    context = {
        "document": document,
        "formset": formset,
        "pending_total": pending.count(),
        "processed": document.entries.exclude(status=ExtractedEntry.Status.PENDING).select_related("transaction")[:200],
    }
    return render(request, "finance/document_review.html", context)


@login_required
def document_download(request, pk):
    document = get_object_or_404(SourceDocument, pk=pk)
    try:
        handle = document.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404("File is no longer available.") from exc
    return FileResponse(handle, as_attachment=True, filename=document.original_name)


def _csv_safe(value):
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@login_required
def reports(request):
    filter_form = TransactionFilterForm(request.GET or None)
    queryset = filter_form.filter(Transaction.objects.all())

    if request.GET.get("export") == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="transactions.csv"'
        writer = csv.writer(response)
        writer.writerow(["Date", "Direction", "Amount", "Description", "Vendor/Payer", "Reference",
                         "Project", "Account", "Category", "Source"])
        for txn in queryset.select_related("project", "account", "category").iterator():
            writer.writerow([
                txn.date.isoformat(), txn.get_direction_display(), f"{txn.amount:.2f}",
                _csv_safe(txn.description), _csv_safe(txn.counterparty), _csv_safe(txn.reference),
                _csv_safe(txn.project.code if txn.project else ""), _csv_safe(txn.account or ""),
                _csv_safe(txn.category or ""), txn.get_source_display(),
            ])
        return response

    totals = _totals(queryset)
    by_project = list(_grouped(queryset, "project__code", "project__name", "project__budget").order_by("project__code"))
    for row in by_project:
        budget = row["project__budget"]
        row["remaining"] = budget - row["expense"] if budget is not None else None
    query = request.GET.copy()
    query.pop("export", None)
    context = {
        "filter_form": filter_form,
        "totals": totals,
        "net": totals["income"] - totals["expense"],
        "by_month": _grouped(queryset.annotate(month=TruncMonth("date")), "month").order_by("month"),
        "by_category": _grouped(queryset, "category__name").order_by("-expense", "-income"),
        "by_project": by_project,
        "by_account": _grouped(queryset, "account__name").order_by("account__name"),
        "top_vendors": queryset.filter(_MONEY_OUT)
        .exclude(counterparty="")
        .values("counterparty")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")[:10],
        "querystring": query.urlencode(),
    }
    return render(request, "finance/reports.html", context)
