import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import extraction
from .models import Account, Category, ExtractedEntry, Project, SourceDocument, Transaction
from .views import _csv_safe


class DashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="accountant", password="safe-test-password")
        self.project = Project.objects.create(code="P-001", name="Sample project", budget=Decimal("100000.00"))
        Transaction.objects.create(
            project=self.project,
            description="Site survey",
            counterparty="Survey Co.",
            amount=Decimal("1250.50"),
            date=date(2026, 1, 15),
        )

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('dashboard')}")

    def test_health_endpoint_is_public(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual((response.status_code, response.content), (200, b"ok"))

    def test_dashboard_shows_project_totals_after_login(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sample project")
        self.assertContains(response, "₹100000.00")
        self.assertContains(response, "₹1250.50")
        self.assertContains(response, "₹98749.50")

    def test_account_balance_includes_opening_balance(self):
        account = Account.objects.create(name="HDFC Current", opening_balance=Decimal("5000.00"))
        Transaction.objects.create(account=account, description="Booking advance", amount=Decimal("2000.00"),
                                   date=date(2026, 1, 20), direction="credit")
        Transaction.objects.create(account=account, description="Cement", amount=Decimal("500.00"),
                                   date=date(2026, 1, 21))
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "₹6500.00")


class ParsingTests(SimpleTestCase):
    def test_parse_amount_formats(self):
        self.assertEqual(extraction.parse_amount("₹1,23,456.50"), Decimal("123456.50"))
        self.assertEqual(extraction.parse_amount("(1,000.00)"), Decimal("-1000.00"))
        self.assertEqual(extraction.parse_amount("250.00 Dr"), Decimal("-250.00"))
        self.assertEqual(extraction.parse_amount("Rs. 99"), Decimal("99.00"))
        self.assertIsNone(extraction.parse_amount("N/A"))
        self.assertIsNone(extraction.parse_amount(""))

    def test_parse_date_formats(self):
        expected = date(2026, 1, 5)
        for value in ("05/01/2026", "05-01-26", "2026-01-05", "05-Jan-2026", "5th January 2026", "Jan 5, 2026",
                      "05/01/2026 10:31:00"):
            self.assertEqual(extraction.parse_date(value), expected, value)
        self.assertIsNone(extraction.parse_date("Opening balance"))

    def test_bank_statement_with_preamble_and_debit_credit_columns(self):
        data = (
            "Account Statement,,,,,\n"
            "Account No: XXXX1234,,,,,\n"
            "Txn Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
            "01/02/2026,UPI-ACME CEMENT,UTR001,\"12,500.00\",,87500.00\n"
            "03/02/2026,NEFT-BUYER BOOKING,UTR002,,\"2,00,000.00\",287500.00\n"
            "Total,,,12500.00,200000.00,\n"
        ).encode()
        result = extraction.extract("bank_statement", "stmt.csv", data)
        self.assertEqual(len(result.rows), 2)
        out, received = result.rows
        self.assertEqual((out.direction, out.amount, out.reference), ("debit", Decimal("12500.00"), "UTR001"))
        self.assertEqual((received.direction, received.amount), ("credit", Decimal("200000.00")))
        self.assertEqual(result.skipped, 1)

    def test_signed_amount_column_infers_direction(self):
        data = b"Date;Description;Amount\n2026-03-01;Steel;-4500\n2026-03-02;Rent received;12000\n"
        rows = extraction.extract("csv", "ledger.csv", data).rows
        self.assertEqual([r.direction for r in rows], ["debit", "credit"])

    def test_missing_header_is_reported(self):
        with self.assertRaises(extraction.ExtractionError):
            extraction.extract("csv", "junk.csv", b"hello,world\n1,2\n")

    def test_invoice_text_heuristics(self):
        text = """ACME Building Supplies Pvt Ltd
        GSTIN: 29ABCDE1234F1Z5
        TAX INVOICE
        Invoice No: INV-2026-042        Invoice Date: 14/03/2026
        Cement 50 bags                  10,000.00
        Sub Total                       10,000.00
        CGST 9%                          900.00
        SGST 9%                          900.00
        Grand Total                  ₹ 11,800.00
        """
        row = extraction.parse_invoice_text(text, "invoice")
        self.assertEqual(row.amount, Decimal("11800.00"))
        self.assertEqual(row.date, date(2026, 3, 14))
        self.assertEqual(row.reference, "INV-2026-042")
        self.assertEqual(row.counterparty, "ACME Building Supplies Pvt Ltd")
        self.assertIn("29ABCDE1234F1Z5", " ".join(row.notes))

    def test_statement_text_lines_use_running_balance(self):
        lines = [
            "01/04/2026 Opening Balance 10,000.00",
            "02/04/2026 ATM WDL 2,000.00 8,000.00",
            "03/04/2026 SALARY CREDIT 5,000.00 13,000.00",
        ]
        rows = extraction.parse_statement_lines(lines)
        self.assertEqual([r.direction for r in rows], ["debit", "credit"])
        self.assertEqual(rows[0].notes, [])

    def test_csv_export_neutralises_formulas(self):
        self.assertEqual(_csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(_csv_safe("Cement"), "Cement")


class ImportFlowTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.user = User.objects.create_user(username="accountant", password="safe-test-password")
        self.client.force_login(self.user)
        self.account = Account.objects.create(name="SBI Current")
        self.project = Project.objects.create(code="P-7", name="Lake View")
        Category.objects.create(name="Materials")

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def upload(self, content, name="ledger.csv", kind="bank_statement"):
        return self.client.post(
            reverse("document_upload"),
            {"kind": kind, "account": self.account.pk, "project": self.project.pk,
             "file": SimpleUploadedFile(name, content, content_type="text/csv")},
        )

    def test_upload_review_and_approve_creates_transactions(self):
        response = self.upload(
            b"Date,Description,Vendor,Category,Debit,Credit\n"
            b"10/05/2026,Cement bags,ACME,materials,5000,\n"
            b"11/05/2026,Flat booking,Mr Rao,,,250000\n"
        )
        document = SourceDocument.objects.get()
        self.assertRedirects(response, reverse("document_review", args=[document.pk]))
        self.assertEqual(document.entries.count(), 2)
        cement = document.entries.get(description="Cement bags")
        self.assertEqual(cement.category.name, "Materials")
        self.assertEqual(cement.project, self.project)

        response = self.client.post(reverse("document_review", args=[document.pk]), {"action": "approve_all"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Transaction.objects.filter(source="bank_statement", account=self.account).count(), 2)
        document.refresh_from_db()
        self.assertEqual(document.status, SourceDocument.Status.COMPLETED)

        report = self.client.get(reverse("reports"))
        self.assertContains(report, "₹250000.00")
        self.assertContains(report, "₹5000.00")

    def test_reupload_overlapping_statement_flags_duplicates(self):
        self.upload(b"Date,Description,Debit,Credit\n10/05/2026,Cement bags,5000,\n")
        self.client.post(reverse("document_review", args=[SourceDocument.objects.get().pk]), {"action": "approve_all"})

        self.upload(b"Date,Description,Debit,Credit\n10/05/2026,Cement bags,5000,\n11/05/2026,Sand,800,\n",
                    name="next.csv")
        document = SourceDocument.objects.latest("uploaded_at")
        self.assertEqual(list(document.entries.values_list("possible_duplicate", flat=True)), [True, False])

        self.client.post(reverse("document_review", args=[document.pk]), {"action": "approve_all"})
        self.assertEqual(Transaction.objects.count(), 2)

    def test_review_formset_edits_and_rejects(self):
        self.upload(b"Date,Description,Amount\n10/05/2026,Cement bags,5000\n11/05/2026,Junk row,1\n")
        document = SourceDocument.objects.get()
        first, second = document.entries.all()
        data = {
            "form-TOTAL_FORMS": "2", "form-INITIAL_FORMS": "2",
            "form-0-id": first.pk, "form-0-decision": "approve", "form-0-date": "2026-05-10",
            "form-0-direction": "debit", "form-0-amount": "5200.00", "form-0-description": "Cement (corrected)",
            "form-1-id": second.pk, "form-1-decision": "reject", "form-1-date": "2026-05-11",
            "form-1-direction": "debit", "form-1-amount": "1", "form-1-description": "Junk row",
        }
        response = self.client.post(reverse("document_review", args=[document.pk]), data)
        self.assertEqual(response.status_code, 302)
        txn = Transaction.objects.get()
        self.assertEqual((txn.amount, txn.description), (Decimal("5200.00"), "Cement (corrected)"))
        second.refresh_from_db()
        self.assertEqual(second.status, ExtractedEntry.Status.REJECTED)

    def test_rejects_mismatched_content_and_duplicate_files(self):
        response = self.upload(b"not a pdf", name="bill.pdf", kind="bill")
        self.assertContains(response, "does not match its extension")

        content = b"Date,Description,Amount\n10/05/2026,Cement,10\n"
        self.upload(content)
        response = self.upload(content, name="again.csv")
        self.assertContains(response, "already been uploaded")
        self.assertEqual(SourceDocument.objects.count(), 1)

    def test_failed_extraction_is_recorded(self):
        self.upload(b"hello,world\n1,2\n")
        document = SourceDocument.objects.get()
        self.assertEqual(document.status, SourceDocument.Status.FAILED)
        self.assertIn("header row", document.error)

    def test_manual_entry(self):
        response = self.client.post(reverse("transaction_create"), {
            "date": "2026-06-01", "direction": "debit", "amount": "1500.00",
            "description": "Site electricity", "project": self.project.pk, "account": self.account.pk,
        })
        self.assertRedirects(response, reverse("transaction_list"))
        txn = Transaction.objects.get()
        self.assertEqual((txn.source, txn.created_by), ("manual", self.user))

    def test_csv_export(self):
        Transaction.objects.create(description="=cmd()", amount=Decimal("10.00"), date=date(2026, 1, 1))
        response = self.client.get(reverse("reports"), {"export": "csv"})
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("'=cmd()", response.content.decode())
