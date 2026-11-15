"""Turn uploaded bills, invoices, bank statements and CSV/Excel files into structured rows.

Everything here is pure parsing (no database access) so it is easy to test. Results are
heuristic: every row is staged for human review before it becomes a ledger transaction.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_ROWS = 5000
MAX_PDF_PAGES = 50
MAX_OCR_PAGES = 5
HEADER_SEARCH_ROWS = 40
MAX_AMOUNT = Decimal("999999999999.99")

DEBIT = "debit"
CREDIT = "credit"


class ExtractionError(Exception):
    """The file could not be turned into rows; the message is safe to show to users."""


@dataclass
class Row:
    date: date | None = None
    amount: Decimal | None = None
    direction: str = ""
    description: str = ""
    counterparty: str = ""
    reference: str = ""
    category_name: str = ""
    project_name: str = ""
    notes: list[str] = field(default_factory=list)
    negative: bool = False


@dataclass
class Result:
    rows: list[Row]
    raw_text: str = ""
    skipped: int = 0


# --------------------------------------------------------------------------- values

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y",
    "%b %d %Y", "%B %d %Y",
)


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[AaPp][Mm])?$", "", text)
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text, flags=re.I)
    text = re.sub(r"\bsept\b", "Sep", text, flags=re.I)
    text = re.sub(r"(?<=[A-Za-z])[-.]|[-.](?=[A-Za-z])", " ", text)
    text = re.sub(r"[,\s]+", " ", text).strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if 1990 <= parsed.year <= 2100:
            return parsed
    return None


_CURRENCY = re.compile(r"₹|\binr\b|\brs\b\.?|\$|,|\s")


def parse_amount(value):
    """Return a signed Decimal (2 dp) or None. Handles 1,234.50 / (1,234.50) / -1234 / 1234.50 Dr."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
        return amount if abs(amount) <= MAX_AMOUNT else None
    text = str(value).strip().lower().rstrip(".")
    if not text:
        return None
    sign = 1
    if text.endswith("dr"):
        sign, text = -1, text[:-2]
    elif text.endswith("cr"):
        text = text[:-2]
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        sign, text = -sign, text[1:-1]
    text = _CURRENCY.sub("", text)
    if text.startswith("-"):
        sign, text = -sign, text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    amount = (Decimal(text) * sign).quantize(Decimal("0.01"))
    return amount if abs(amount) <= MAX_AMOUNT else None


def _text(value, max_length):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())[:max_length]


# --------------------------------------------------------------------------- tables

_HEADER_RULES = (
    ("balance", re.compile(r"balance")),
    ("type", re.compile(r"^(dr/cr|cr/dr|debit/credit|type|transaction type|txn type)$")),
    ("debit", re.compile(r"withdrawal|debit|^dr$|money out|paid out")),
    ("credit", re.compile(r"deposit|credit|^cr$|money in|paid in")),
    ("date", re.compile(r"date")),
    ("reference", re.compile(r"\bref|chq|cheque|\butr\b|invoice no|bill no")),
    ("category", re.compile(r"categor|expense head|ledger")),
    ("project", re.compile(r"^project")),
    ("description", re.compile(r"narration|description|particular|detail|remark|memo")),
    ("counterparty", re.compile(r"vendor|payee|party|merchant|beneficiary|supplier|customer")),
    ("amount", re.compile(r"amount|\bamt\b")),
)


def _normalise_header(value):
    text = str(value or "").lower()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z/]+", " ", text)
    text = re.sub(r"\s*/\s*", "/", text)
    return " ".join(text.split())


def _map_header(cells):
    mapping = {}
    for index, cell in enumerate(cells):
        name = _normalise_header(cell)
        if not name:
            continue
        for field_name, pattern in _HEADER_RULES:
            if pattern.search(name):
                mapping.setdefault(field_name, index)
                break
    return mapping


def _is_header(mapping):
    return "date" in mapping and bool({"amount", "debit", "credit"} & mapping.keys())


def _cell(cells, mapping, key):
    index = mapping.get(key)
    if index is None or index >= len(cells):
        return None
    return cells[index]


def _parse_table_row(cells, mapping):
    txn_date = parse_date(_cell(cells, mapping, "date"))
    if txn_date is None:
        return None
    debit = parse_amount(_cell(cells, mapping, "debit"))
    credit = parse_amount(_cell(cells, mapping, "credit"))
    row = Row(date=txn_date)
    if debit or credit:
        if debit and credit:
            row.notes.append("Row had both debit and credit values; net amount used.")
        net = abs(credit or 0) - abs(debit or 0)
        if not net:
            return None
        row.direction = CREDIT if net > 0 else DEBIT
        row.amount = abs(net)
    else:
        amount = parse_amount(_cell(cells, mapping, "amount"))
        if not amount:
            return None
        kind = _text(_cell(cells, mapping, "type"), 20).lower()
        if kind.startswith(("dr", "debit", "withdraw", "out", "expense", "payment")):
            row.direction = DEBIT
        elif kind.startswith(("cr", "credit", "deposit", "in", "income", "receipt")):
            row.direction = CREDIT
        row.amount = abs(amount)
        row.negative = amount < 0

    row.description = _text(_cell(cells, mapping, "description"), 250)
    row.counterparty = _text(_cell(cells, mapping, "counterparty"), 200)
    row.reference = _text(_cell(cells, mapping, "reference"), 100)
    row.category_name = _text(_cell(cells, mapping, "category"), 100)
    row.project_name = _text(_cell(cells, mapping, "project"), 200)
    if not row.description:
        row.description = row.counterparty or "Imported transaction"
    return row


def _infer_directions(rows, default_direction):
    unknown = [row for row in rows if not row.direction]
    if not unknown:
        return
    if any(row.negative for row in unknown):
        for row in unknown:
            row.direction = DEBIT if row.negative else CREDIT
            row.notes.append("Money in/out inferred from the amount's sign.")
    else:
        for row in unknown:
            row.direction = default_direction
            row.notes.append("No debit/credit column found; assumed money out - please verify.")


def parse_table(rows, default_direction=DEBIT):
    """Parse rows of cells (from CSV, Excel or PDF tables) that contain a header row."""
    mapping = None
    parsed, skipped = [], 0
    for position, raw_cells in enumerate(rows):
        cells = [" ".join(c.split()) if isinstance(c, str) else c for c in (raw_cells or [])]
        if mapping is None:
            candidate = _map_header(cells)
            if _is_header(candidate):
                mapping = candidate
            elif position >= HEADER_SEARCH_ROWS:
                break
            continue
        if not any(c not in ("", None) for c in cells):
            continue
        row = _parse_table_row(cells, mapping)
        if row is None:
            skipped += 1
            continue
        parsed.append(row)
        if len(parsed) > MAX_ROWS:
            raise ExtractionError(f"The file has more than {MAX_ROWS} transactions; please split it.")
    if mapping is None:
        raise ExtractionError(
            "Could not find a header row with a date column and an amount (or debit/credit) column."
        )
    _infer_directions(parsed, default_direction)
    return parsed, skipped


def read_csv(data):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")
    if "\x00" in text:
        raise ExtractionError("This does not look like a text CSV file.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        return list(csv.reader(io.StringIO(text), dialect))
    except csv.Error as exc:
        raise ExtractionError(f"Could not read the CSV file: {exc}") from exc


def read_xlsx(data):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError("Could not open the Excel file (.xlsx).") from exc
    try:
        sheet = workbook.worksheets[0]
        return [list(r) for r in sheet.iter_rows(values_only=True, max_row=MAX_ROWS + 200, max_col=60)]
    finally:
        workbook.close()


# --------------------------------------------------------------------------- PDF / OCR

def _read_pdf(data, with_tables):
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ExtractionError(f"PDF has more than {MAX_PDF_PAGES} pages; please split it.")
            texts, tables = [], []
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                if with_tables:
                    for table in page.extract_tables():
                        tables.extend(table)
            return "\n".join(texts), tables
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("Could not read the PDF (it may be damaged or password-protected).") from exc


def _ocr(image):
    import pytesseract

    try:
        return pytesseract.image_to_string(image.convert("L"), timeout=60)
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractionError("The OCR engine (Tesseract) is not installed on the server.") from exc
    except RuntimeError as exc:
        raise ExtractionError("OCR timed out while reading this document.") from exc


def _ocr_pdf(data):
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            images = [page.to_image(resolution=200).original for page in pdf.pages[:MAX_OCR_PAGES]]
    except Exception as exc:
        raise ExtractionError("Could not render the scanned PDF for OCR.") from exc
    return "\n".join(_ocr(image) for image in images)


def _ocr_image_bytes(data):
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ExtractionError("Could not read the image file.") from exc
    with image:
        return _ocr(image)


# --------------------------------------------------------------------------- statement text lines

_LINE_START = re.compile(
    r"^(?P<date>\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}[ \-][A-Za-z]{3}[ \-]\d{2,4})\s+(?P<rest>.+)$"
)
_STATEMENT_AMOUNT = re.compile(r"\(?-?(?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2}\)?(?:\s*(?:Dr|Cr)\b)?", re.I)


def parse_statement_lines(lines):
    """Fallback for PDF statements without detectable tables: `date description amount balance`."""
    rows, previous_balance = [], None
    for line in lines:
        match = _LINE_START.match(line.strip())
        if not match:
            continue
        txn_date = parse_date(match["date"])
        tokens = list(_STATEMENT_AMOUNT.finditer(match["rest"]))
        values = [parse_amount(token.group()) for token in tokens]
        if txn_date is None or not values or None in values:
            continue
        row = Row(date=txn_date, description=_text(match["rest"][: tokens[0].start()], 250))
        if re.search(r"opening balance|balance b/?f|brought forward", row.description, re.I):
            previous_balance = values[-1]
            continue
        if len(values) >= 2:
            amount, balance = abs(values[-2]), values[-1]
            if previous_balance is not None and abs(previous_balance - amount - balance) < Decimal("0.01"):
                row.direction = DEBIT
            elif previous_balance is not None and abs(previous_balance + amount - balance) < Decimal("0.01"):
                row.direction = CREDIT
            else:
                row.direction = DEBIT
                row.notes.append("Could not confirm money in/out from the running balance - please verify.")
            previous_balance = balance
        else:
            amount = abs(values[0])
            row.direction = DEBIT if values[0] < 0 else CREDIT
            row.notes.append("Money in/out guessed from a single amount column - please verify.")
        if not amount:
            continue
        row.amount = amount
        row.description = row.description or "Bank transaction"
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ExtractionError(f"The statement has more than {MAX_ROWS} transactions; please split it.")
    return rows


# --------------------------------------------------------------------------- bills / invoices

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_IN_TEXT = re.compile(
    rf"\b(\d{{1,2}}[/\-.]\d{{1,2}}[/\-.]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+{_MONTHS}[\s\-,]+\d{{2,4}}|{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}})\b",
    re.I,
)
_MONEY = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?(?![\d])")
_TOTAL_KEYS = (
    "grand total", "total amount payable", "amount payable", "net payable", "total payable",
    "invoice total", "total due", "balance due", "amount due", "net amount", "total amount", "total",
)
_DATE_KEYS = ("invoice date", "bill date", "date of issue", "inv date", "receipt date", "dated", "date")
_REFERENCE = re.compile(
    r"(?:invoice|inv|bill|receipt)\s*(?:no|number|num|#)\.?\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9/\-]{0,40})", re.I
)
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")
_TITLE_WORDS = re.compile(r"invoice|receipt|bill of supply|cash memo|original|duplicate|page \d|estimate", re.I)


def _amounts_in(text):
    values = [parse_amount(token) for token in _MONEY.findall(text)]
    return [value for value in values if value and value > 0]


def _find_total(lines):
    for key in _TOTAL_KEYS:
        for index in range(len(lines) - 1, -1, -1):
            lower = lines[index].lower()
            if key not in lower:
                continue
            if key == "total" and re.search(r"sub\s*-?\s*total|total\s+(tax|gst|qty|quantity|items|discount)", lower):
                continue
            amounts = _amounts_in(lines[index][lower.index(key) + len(key):])
            if not amounts and index + 1 < len(lines):
                amounts = _amounts_in(lines[index + 1])
            if amounts:
                return amounts[-1]
    return None


def _find_date(lines):
    for key in _DATE_KEYS:
        for index, line in enumerate(lines):
            lower = line.lower()
            if key not in lower:
                continue
            segment = line[lower.index(key) + len(key):]
            if index + 1 < len(lines):
                segment += "\n" + lines[index + 1]
            for match in _DATE_IN_TEXT.finditer(segment):
                parsed = parse_date(match.group())
                if parsed:
                    return parsed
    for match in _DATE_IN_TEXT.finditer("\n".join(lines)):
        parsed = parse_date(match.group())
        if parsed:
            return parsed
    return None


def _find_vendor(lines):
    for line in lines[:8]:
        if len(re.findall(r"[A-Za-z]", line)) >= 3 and not _TITLE_WORDS.search(line):
            return _text(line, 200)
    return ""


def parse_invoice_text(text, kind):
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    row = Row(direction=DEBIT)
    row.amount = _find_total(lines)
    if row.amount is None:
        candidates = [parse_amount(t) for line in lines for t in _MONEY.findall(line) if "." in t]
        candidates = [a for a in candidates if a and a > 0]
        if candidates:
            row.amount = max(candidates)
            row.notes.append("No labelled total found; used the largest amount - please verify.")
        else:
            row.notes.append("Total amount not found.")
    row.date = _find_date(lines)
    if row.date is None:
        row.notes.append("Date not found.")
    reference = next((m.group(1) for m in _REFERENCE.finditer(text) if re.search(r"\d", m.group(1))), "")
    row.reference = reference[:100]
    row.counterparty = _find_vendor(lines)
    if row.counterparty:
        row.notes.append("Vendor guessed from the document header.")
    gstins = sorted(set(_GSTIN.findall(text)))
    if gstins:
        row.notes.append("GSTIN: " + ", ".join(gstins[:3]))
    label = "Invoice" if kind == "invoice" else "Bill"
    description = f"{label} {row.reference}".strip()
    if row.counterparty:
        description += f" from {row.counterparty}"
    row.description = description[:250]
    return row


# --------------------------------------------------------------------------- entry point

def extract(kind, filename, data):
    """Extract rows from an uploaded file. `kind` is a SourceKind value."""
    suffix = Path(filename).suffix.lower()
    if kind in ("bill", "invoice"):
        if suffix == ".pdf":
            text, _ = _read_pdf(data, with_tables=False)
            if len(text.strip()) < 20:
                text = _ocr_pdf(data)
        else:
            text = _ocr_image_bytes(data)
        if not text.strip():
            raise ExtractionError("No readable text was found in the document.")
        return Result(rows=[parse_invoice_text(text, kind)], raw_text=text)

    if suffix == ".pdf":
        text, tables = _read_pdf(data, with_tables=True)
        if tables:
            try:
                rows, skipped = parse_table(tables)
            except ExtractionError:
                rows, skipped = [], 0
            if rows:
                return Result(rows=rows, raw_text=text, skipped=skipped)
        rows = parse_statement_lines(text.splitlines())
        if not rows:
            raise ExtractionError(
                "No transactions found in the PDF. If it is scanned, download the CSV/Excel statement instead."
            )
        return Result(rows=rows, raw_text=text)

    table = read_xlsx(data) if suffix == ".xlsx" else read_csv(data)
    rows, skipped = parse_table(table)
    if not rows:
        raise ExtractionError("The file had a header row but no valid transactions.")
    return Result(rows=rows, skipped=skipped)
