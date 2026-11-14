from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

CENT = Decimal("0.01")

# Mirrors NUMERIC(12, 2) in schema.sql and the CHECK (>= 0) constraints
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class LineItemExtraction(BaseModel):
    """One row of the invoice's line-item table, exactly as printed."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    description: str = Field(
        min_length=1, max_length=255,
        description="What was sold, copied from the row. Example: 'Web Design'.",
    )
    quantity: Decimal = Field(
        gt=0, max_digits=10, decimal_places=4,
        description="Units or hours as a plain number. Example: 2.5",
    )
    unit_price: Money = Field(
        description="Price of ONE unit, as a plain number without currency symbol. Example: 250.00",
    )
    line_total: Money = Field(
        description="Total for this row as printed on the invoice. Example: 625.00",
    )

    @model_validator(mode="after")
    def _line_total_matches(self):
        expected = (self.quantity * self.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        if expected != self.line_total:
            raise ValueError(
                f"line_total {self.line_total} != quantity x unit_price = {expected} "
                f"for item {self.description!r}"
            )
        return self


class InvoiceExtraction(BaseModel):
    """One invoice, as printed in the document. Use null when a value is not shown. Never guess."""
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={"examples": [{
            "vendor_name": "Acme Ltd",
            "vendor_tax_id": None,
            "invoice_number": "INV-1",
            "issue_date": "2026-01-25",
            "due_date": "2026-01-31",
            "currency": "USD",
            "subtotal": "85.00",
            "tax": "8.50",
            "total": "93.50",
            "line_items": [{"description": "Web Design", "quantity": "1.00",
                            "unit_price": "85.00", "line_total": "85.00"}],
        }]},
    )

    vendor_name: str = Field(
        min_length=1, max_length=100,
        description="The seller who issued the invoice (the 'From' party), not the customer.",
    )
    vendor_tax_id: str | None = Field(
        max_length=100,
        description="Vendor tax, VAT or ABN number. null if the document does not show one.",
    )
    invoice_number: str = Field(
        min_length=1, max_length=100,
        description="The invoice's own number (e.g. 'INV-3337'), not an order or PO number.",
    )
    issue_date: date = Field(description="Invoice date in ISO format YYYY-MM-DD.")
    due_date: date = Field(description="Payment due date in ISO format YYYY-MM-DD.")
    currency: str | None = Field(
        pattern=r"^[A-Z]{3}$",
        description="ISO 4217 code such as USD or AUD. null if only a symbol like '$' is shown. Do not guess.",
    )
    subtotal: Money = Field(description="Sum of line totals before tax, as a plain number.")
    tax: Money = Field(description="Total tax amount (not the rate), as a plain number.")
    total: Money = Field(description="Final amount due, as a plain number.")
    line_items: list[LineItemExtraction] = Field(
        min_length=1, description="Every row of the line-item table, in order.",
    )

    @model_validator(mode="after")
    def _cross_checks(self):
        if self.due_date < self.issue_date:
            raise ValueError(f"due_date {self.due_date} is before issue_date {self.issue_date}")
        if self.subtotal + self.tax != self.total:
            raise ValueError(f"total {self.total} != subtotal {self.subtotal} + tax {self.tax}")
        line_sum = sum((i.line_total for i in self.line_items), Decimal("0"))
        if line_sum != self.subtotal:
            raise ValueError(f"subtotal {self.subtotal} != sum of line totals {line_sum}")
        return self
    