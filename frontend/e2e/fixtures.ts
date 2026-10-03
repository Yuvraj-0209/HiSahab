/* Sample API responses for the smoke suite (Phase 23 D10).
 *
 * Every fixture is typed against the generated OpenAPI types, so a field the server renames,
 * drops or makes nullable fails `tsc` here instead of leaving the smoke suite exercising a shape
 * the server no longer sends. The figures are synthetic -- a plausible trading day at one pump --
 * and money is a string throughout, exactly as on the wire.
 */

import type { Schemas } from "../src/api/types";

export const OUTLET_ID = "00000000-0000-0000-0000-000000000001";
export const ME_ID = "5f6c1a2e-0000-4000-8000-000000000001";
export const SHIFT_ID = "a1b2c3d4-0000-4000-8000-000000000101";
export const CUSTOMER_ID = "c0ffee00-0000-4000-8000-000000000201";
export const NOZZLE_ID = "b0b0b0b0-0000-4000-8000-000000000301";

export const BUSINESS_DATE = "2026-10-02";

export function me(role: Schemas["MeResponse"]["role"] = "admin"): Schemas["MeResponse"] {
  return { id: ME_ID, full_name: "Harpreet Sandhu", phone: "9876543210", outlet_id: OUTLET_ID, role };
}

export const clientConfig: Schemas["ClientConfigResponse"] = {
  tz_display: "Asia/Kolkata",
  outlet_name: "Sandhu Fuels, Kharar",
  expense_review_threshold: "1000.00",
  expense_receipt_threshold: "5000.00",
  variance_alert_threshold: "100.00",
  max_upload_bytes: 5242880,
};

export function shift(status: Schemas["ShiftResponse"]["status"] = "open"): Schemas["ShiftResponse"] {
  return {
    id: SHIFT_ID,
    outlet_id: OUTLET_ID,
    business_date: BUSINESS_DATE,
    sequence: 1,
    started_at: "2026-10-02T00:30:00Z",
    ended_at: status === "open" ? null : "2026-10-02T16:30:00Z",
    attendant_id: ME_ID,
    status,
    closed_by: status === "open" ? null : ME_ID,
    closed_at: status === "open" ? null : "2026-10-02T16:45:00Z",
    locked_by: status === "locked" ? ME_ID : null,
    locked_at: status === "locked" ? "2026-10-03T03:00:00Z" : null,
  };
}

export const sales: Schemas["ShiftSales"] = {
  shift_id: SHIFT_ID,
  business_date: BUSINESS_DATE,
  priced_at: "2026-10-02T00:30:00Z",
  lines: [],
  quantity_by_unit: { litre: "2841.370", kilogram: "212.400" },
  total_sale_value: "302827.45",
  total_gross_fuel_margin: "9873.12",
  incomplete: false,
  margin_basis: "gross fuel margin on quantity sold",
};

export const collections: Schemas["CollectionPage"] = {
  items: [
    { id: "col-1", shift_id: SHIFT_ID, mode: "cash", amount: "17600.00", reference: null, reverses_id: null, reversal_reason: null, is_reversed: false },
    { id: "col-2", shift_id: SHIFT_ID, mode: "card", amount: "98410.00", reference: "BATCH 4471", reverses_id: null, reversal_reason: null, is_reversed: false },
    { id: "col-3", shift_id: SHIFT_ID, mode: "upi", amount: "167207.00", reference: null, reverses_id: null, reversal_reason: null, is_reversed: false },
  ],
  declared_cash: "17600.00",
  totals_by_mode: { cash: "17600.00", card: "98410.00", upi: "167207.00" },
  cash_basis: "declared",
  truncated: false,
};

export const expenses: Schemas["ExpensePage"] = {
  items: [
    {
      id: "exp-1", shift_id: SHIFT_ID, category_id: "cat-1", category_code: "TEA", mode: "cash", amount: "240.00",
      description: "Tea for the forecourt", paid_to: null, attachment_id: null, receipt_required: false,
      reverses_id: null, reversal_reason: null, is_reversed: false, requires_review: false, reviewed_by: null,
      reviewed_at: null, review_note: null,
    },
    {
      id: "exp-2", shift_id: SHIFT_ID, category_id: "cat-2", category_code: "MAINTENANCE", mode: "cash", amount: "1850.00",
      description: "Dispenser belt replaced", paid_to: "Gill Engineering", attachment_id: null, receipt_required: false,
      reverses_id: null, reversal_reason: null, is_reversed: false, requires_review: true, reviewed_by: null,
      reviewed_at: null, review_note: null,
    },
  ],
  total: "2090.00",
  totals_by_category: { TEA: "240.00", MAINTENANCE: "1850.00" },
  truncated: false,
};

export const creditSales: Schemas["CreditSalePage"] = {
  items: [
    {
      id: "cs-1", shift_id: SHIFT_ID, credit_customer_id: CUSTOMER_ID, fuel_type_id: null, quantity: null,
      amount: "19610.00", vehicle_number: "PB65AX1204", attachment_id: "att-1", limit_override_reason: null,
      reverses_id: null, reversal_reason: null, is_reversed: false, created_at: "2026-10-02T09:12:00Z",
    },
  ],
  total: "19610.00",
  truncated: false,
};

export const repayments: Schemas["CreditRepaymentPage"] = {
  items: [
    {
      id: "cr-1", shift_id: SHIFT_ID, business_date: BUSINESS_DATE, credit_customer_id: CUSTOMER_ID, mode: "cash",
      amount: "5000.00", attachment_id: null, reverses_id: null, reversal_reason: null, is_reversed: false,
      created_at: "2026-10-02T11:40:00Z",
    },
  ],
  total: "5000.00",
  cash_total: "5000.00",
  card_upi_total: "0.00",
  truncated: false,
};

export const customers: Schemas["CreditCustomerListItem"][] = [
  { id: CUSTOMER_ID, name: "Bhullar Transport", vehicle_numbers: ["PB65AX1204"], is_active: true },
];

export const deposits: Schemas["BankDepositPage"] = { items: [], total: "0.00", cash_basis: "deposited", truncated: false };

export const dayCash: Schemas["DayCashResponse"] = {
  source: "computed",
  shift_count: 1,
  incomplete: false,
  is_finalised: false,
  requires_review: false,
  review_note: null,
  unavailable_reason: null,
  opening_balance: "48210.00",
  opening_balance_source: "carried",
  metered_fuel_sales: "302827.45",
  non_fuel_sales_total: "0.00",
  total_sales: "302827.45",
  card_total: "98410.00",
  upi_total: "167207.00",
  wallet_total: "0.00",
  credit_sales_total: "19610.00",
  card_upi_credit_repayments: "0.00",
  cash_credit_repayments: "5000.00",
  cash_shortfall_settlements: "0.00",
  cash_expenses: "2090.00",
  bank_deposits_total: "0.00",
  shortfalls_booked: "0.00",
  expected_closing: "68720.45",
  actual_counted: null,
  variance: null,
};

/** One petrol nozzle with a confirmed opening and a closing, one CBG nozzle still awaiting its
 * closing -- the two states the worksheet spends most of a day in. */
export const worksheet: Schemas["Worksheet"] = {
  shift_id: SHIFT_ID,
  shift_status: "open",
  lines: [
    {
      nozzle_id: NOZZLE_ID,
      nozzle_label: "DU-1/N-1",
      dispenser_label: "DU-1",
      fuel_type_code: "PETROL",
      unit_of_measure: "litre",
      totalizer_max_value: "999999.99",
      chained_opening_reading: "418236.40",
      requires_anchor: false,
      reading: {
        id: "rd-1", shift_id: SHIFT_ID, nozzle_id: NOZZLE_ID, nozzle_label: "DU-1/N-1", fuel_type_code: "PETROL",
        unit_of_measure: "litre", opening_reading: "418236.40", chained_opening_reading: "418236.40",
        opening_variance_reason: null, closing_reading: "419512.75", testing_quantity: "5.000",
        rollover_occurred: false, meter_reset_occurred: false, manual_quantity_override: null, override_reason: null,
        quantity_sold: "1271.350", requires_review: false, reviewed_by: null, reviewed_at: null, review_note: null,
      },
    },
    {
      nozzle_id: "b0b0b0b0-0000-4000-8000-000000000302",
      nozzle_label: "CBG-1",
      dispenser_label: "CBG",
      fuel_type_code: "CBG",
      unit_of_measure: "kilogram",
      totalizer_max_value: "99999.99",
      chained_opening_reading: "61822.10",
      requires_anchor: false,
      reading: null,
    },
  ],
};

export const nonFuelSales: Schemas["NonFuelSalePage"] = {
  items: [
    { id: "nf-1", shift_id: SHIFT_ID, amount: "480.00", description: "Engine oil, 1 L", reverses_id: null, reversal_reason: null, is_reversed: false },
  ],
  total: "480.00",
  sales_basis: "added to total sales",
  truncated: false,
};

export const categories: Schemas["ExpenseCategoryResponse"][] = [
  { id: "cat-1", code: "TEA", display_name: "Tea", requires_receipt: false, is_active: true },
  { id: "cat-2", code: "MAINTENANCE", display_name: "Maintenance", requires_receipt: false, is_active: true },
  { id: "cat-3", code: "OTHER", display_name: "Other", requires_receipt: true, is_active: true },
];

export const fuelTypes: Schemas["FuelTypeResponse"][] = [
  { id: "ft-1", code: "PETROL", display_name: "Petrol", unit_of_measure: "litre", max_flow_rate_per_minute: "60.000", is_active: true },
  { id: "ft-2", code: "CBG", display_name: "CBG", unit_of_measure: "kilogram", max_flow_rate_per_minute: "15.000", is_active: true },
];

/** The 30 July shape (§6.4): every term present, a ₹500 gap the salesman is short by. */
export const cashPosition: Schemas["CashPositionResponse"] = {
  shift_id: SHIFT_ID,
  business_date: BUSINESS_DATE,
  salesman_id: ME_ID,
  incomplete: false,
  metered_fuel_sales: "302827.45",
  non_fuel_sales: "480.00",
  card_upi_credit_repayments: "0.00",
  card_total: "98410.00",
  upi_total: "167207.00",
  wallet_total: "0.00",
  credit_sales_total: "19610.00",
  cash_credit_repayments: "5000.00",
  cash_shortfall_settlements: "0.00",
  accountable_cash: "23080.45",
  declared_cash: "22580.45",
  gap: "500.00",
  cash_expenses: "2090.00",
  shortfalls_booked: "0.00",
  gap_basis: "accountable minus declared",
  margin_basis: "not used",
};
