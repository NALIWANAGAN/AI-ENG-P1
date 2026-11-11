
create table if not exists vendors (
    id uuid primary key default gen_random_uuid(),
    name varchar(100) not null,
    tax_id varchar(100),

    constraint vendors_name_not_empty  check (btrim(name) <> '')
);


create table if not exists invoices (
    id uuid primary key default gen_random_uuid(),
    vendor_id uuid not null references vendors(id) on delete restrict,
    invoice_number varchar(100) not null,
    issue_date date not null,
    due_date date not null,
    currency varchar(3) not null,
    subtotal numeric(12, 2) not null,
    tax numeric(12, 2) not null,
    total numeric(12, 2) not null,

    constraint invoices_vendor_number_unique UNIQUE (vendor_id, invoice_number),
    constraint invoices_number_not_empty     check (btrim(invoice_number) <> ''),
    constraint invoices_due_after_issue      check (due_date >= issue_date),
    constraint invoices_currency_format      check (currency ~ '^[A-Z]{3}$'),
    constraint invoices_amounts_nonneg       check (subtotal >= 0 and tax >= 0 and total >= 0),
    constraint invoices_total_matches        check (total = subtotal + tax)
);


create table if not exists line_items (
    id uuid primary key default gen_random_uuid(),
    invoice_id uuid not null references invoices(id) on delete cascade,
    description varchar(255) not null,
    quantity numeric(10, 4) not null,
    unit_price numeric(12, 2) not null,
    line_total numeric (12, 2) not null,

    constraint line_items_description_not_empty check (btrim(description) <> ''),
    constraint line_items_quantity_positive     check (quantity > 0),
    constraint line_items_unit_price_nonneg     check (unit_price >= 0),
    constraint line_items_total_matches         check (line_total = ROUND(quantity * unit_price, 2))
);

create index if not exists idx_line_items_invoice_id on line_items(invoice_id);
