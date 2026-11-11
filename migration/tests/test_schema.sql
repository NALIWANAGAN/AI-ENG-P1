BEGIN;

INSERT INTO vendors (name) VALUES ('Test Vendor');

-- should FAIL: invoices_currency_format
INSERT INTO invoices (vendor_id, invoice_number, issue_date, due_date, currency, subtotal, tax, total)
VALUES ((SELECT id FROM vendors WHERE name = 'Test Vendor'),
        'INV-1', '2026-01-01', '2026-01-31', 'USD', 100, 10, 110);

ROLLBACK;
