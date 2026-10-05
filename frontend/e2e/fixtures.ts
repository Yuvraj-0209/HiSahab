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

/* --- the Cash tab: three days in three different states ----------------------------------
 *   30 Sep  locked, reconciled, counted ₹200 short (§6.5's worked example)
 *   1 Oct   locked, never reconciled -- the oldest unreconciled day, so the only one offered
 *           "Reconcile this day" (§6.5)
 *   2 Oct   still open: entry in progress
 */

export const PREVIOUS_DATE = "2026-10-01";
export const RECONCILED_DATE = "2026-09-30";

function lockedShift(id: string, business_date: string): Schemas["ShiftResponse"] {
  return {
    ...shift("locked"),
    id,
    business_date,
    started_at: `${business_date}T00:30:00Z`,
    ended_at: `${business_date}T16:30:00Z`,
  };
}

export const shiftPage: Schemas["ShiftPage"] = {
  items: [shift("open"), lockedShift("shift-oct1", PREVIOUS_DATE), lockedShift("shift-sep30", RECONCILED_DATE)],
  next_cursor: null,
};

export const summaryPage: Schemas["SummaryPage"] = {
  truncated: false,
  items: [
    {
      id: "sum-sep30",
      business_date: RECONCILED_DATE,
      opening_balance: "45000.00",
      opening_balance_source: "carried",
      metered_fuel_sales: "288410.00",
      non_fuel_sales_total: "0.00",
      card_total: "90120.00",
      upi_total: "160300.00",
      wallet_total: "0.00",
      credit_sales_total: "12000.00",
      card_upi_credit_repayments: "0.00",
      cash_credit_repayments: "0.00",
      cash_shortfall_settlements: "0.00",
      cash_expenses: "1780.00",
      bank_deposits_total: "0.00",
      shortfalls_booked: "0.00",
      expected_closing: "69210.00",
      actual_counted: "69010.00",
      variance: "-200.00",
      variance_basis: "counted minus expected",
      is_finalised: false,
      requires_review: false,
      review_note: null,
      notes: null,
    },
  ],
};

export const dailyReport: Schemas["DailyReportResponse"] = {
  business_date: BUSINESS_DATE,
  cash: dayCash,
  cash_basis: "computed live: this day has not been reconciled",
  fuel: [
    {
      fuel_type_id: "ft-1", code: "PETROL", display_name: "Petrol", unit_of_measure: "litre", quantity: "2841.370",
      rate_per_unit: "94.72", sale_value: "269134.57", margin_per_unit: "3.99", gross_fuel_margin: "11337.07",
      margin_unavailable_reason: null,
    },
    {
      fuel_type_id: "ft-2", code: "CBG", display_name: "CBG", unit_of_measure: "kilogram", quantity: "212.400",
      rate_per_unit: "86.00", sale_value: "18266.40", margin_per_unit: null, gross_fuel_margin: null,
      margin_unavailable_reason: "NO_MARGIN_FOR_DATE",
    },
  ],
  fuel_basis: "valued at the rate effective at each shift's start",
  fuel_sales_total: "287400.97",
  gross_fuel_margin_total: null,
  fuels_missing_margin: ["CBG"],
  profit_basis: "Gross fuel margin on quantity sold, not business profit. It excludes stock revaluation.",
  quantity_by_unit: { litre: "2841.370", kilogram: "212.400" },
  expenses_by_category: { TEA: "240.00", MAINTENANCE: "1850.00" },
  expenses_total: "2090.00",
  shifts: [{ id: SHIFT_ID, sequence: 1, status: "open", attendant_id: ME_ID, started_at: "2026-10-02T00:30:00Z", ended_at: null }],
  breakdown_reconciles: null,
  snapshot_metered_fuel_sales: null,
};

function rangeDay(business_date: string, extra: Partial<Schemas["RangeDayResponse"]>): Schemas["RangeDayResponse"] {
  return {
    business_date, source: "computed", shift_count: 1, is_finalised: false, requires_review: false, alert: false,
    total_sales: "290000.00", metered_fuel_sales: "290000.00", non_fuel_sales_total: "0.00", expected_closing: null,
    actual_counted: null, variance: null, unavailable_reason: null, bar_height_pct: "95.00%", ...extra,
  };
}

export const rangeReport: Schemas["RangeReportResponse"] = {
  from: "2026-09-26",
  to: BUSINESS_DATE,
  threshold: "100.00",
  basis: "Snapshot days are read as stored; unreconciled days are computed now and can still move.",
  days: [
    rangeDay("2026-09-26", { source: "no_trading", total_sales: null, metered_fuel_sales: null, bar_height_pct: "0.00%", shift_count: 0 }),
    rangeDay("2026-09-27", { total_sales: "301220.00", bar_height_pct: "99.47%" }),
    rangeDay("2026-09-28", { total_sales: "276110.00", bar_height_pct: "91.18%" }),
    rangeDay("2026-09-29", { total_sales: "260004.00", bar_height_pct: "85.86%" }),
    rangeDay(RECONCILED_DATE, { source: "snapshot", total_sales: "288410.00", bar_height_pct: "95.24%", variance: "-200.00", alert: true, expected_closing: "69210.00", actual_counted: "69010.00" }),
    rangeDay(PREVIOUS_DATE, { total_sales: "282001.00", bar_height_pct: "93.12%" }),
    rangeDay(BUSINESS_DATE, { total_sales: "302827.45", bar_height_pct: "100.00%" }),
  ],
};

export const alerts: Schemas["AlertsResponse"] = {
  from: "2026-09-26",
  to: BUSINESS_DATE,
  threshold: "100.00",
  basis: "Every alert is derived from a stored signal on each read.",
  items: [
    { kind: "variance_exceeds_threshold", business_date: RECONCILED_DATE, detail: "Counted ₹200.00 below expected.", amount: "-200.00", count: null, shift_id: null },
    { kind: "day_not_reconciled", business_date: PREVIOUS_DATE, detail: "Every shift is locked and no summary exists.", amount: null, count: null, shift_id: null },
  ],
};

export const SALESMAN_ID = "5f6c1a2e-0000-4000-8000-000000000002";

export const shortfallOutstanding: Schemas["OutstandingReport"] = {
  basis: "booked shortfalls less settlements, reversals included",
  items: [{ salesman_id: SALESMAN_ID, full_name: "Gurpreet Singh", outstanding: "500.00" }],
};

export const shortfallLedger: Schemas["app__api__v1__shortfalls__LedgerPage"] = {
  next_cursor: null,
  items: [
    { id: "le-1", kind: "shortfall", amount: "700.00", balance_delta: "700.00", is_reversal: false, created_at: "2026-09-28T17:05:00Z", shift_id: "shift-sep28" },
    { id: "le-2", kind: "settlement", amount: "200.00", balance_delta: "-200.00", is_reversal: false, created_at: "2026-09-30T06:40:00Z", shift_id: "shift-sep30" },
  ],
};

export const flaggedPage: Schemas["FlaggedExpensePage"] = {
  next_cursor: null,
  items: [
    { id: "fx-1", shift_id: "shift-sep30", business_date: RECONCILED_DATE, category_id: "cat-2", category_code: "MAINTENANCE", amount: "600.00", description: "Nozzle hose clamp" },
    { id: "fx-2", shift_id: "shift-sep30", business_date: RECONCILED_DATE, category_id: "cat-2", category_code: "MAINTENANCE", amount: "650.00", description: "Electrician call-out" },
  ],
};

/* --- the Credit tab ----------------------------------------------------------------------- */

export const CUSTOMER_2 = "c0ffee00-0000-4000-8000-000000000202";
export const CUSTOMER_3 = "c0ffee00-0000-4000-8000-000000000203";

export const moreCustomers: Schemas["CreditCustomerListItem"][] = [
  { id: CUSTOMER_2, name: "Sandhu Dairy", vehicle_numbers: null, is_active: true },
  { id: CUSTOMER_3, name: "Old Mill Traders", vehicle_numbers: null, is_active: false },
];

/** Bhullar is anchored with history; Sandhu has never had an opening balance entered; Old Mill
 * was entered at exactly ₹0.00 -- the three states §6.8 says must read differently. */
export const openingBalances: Schemas["OpeningBalancePage"] = {
  items: [
    {
      credit_customer_id: CUSTOMER_ID, name: "Bhullar Transport", is_active: true, opening_balance: "12400.00",
      opening_balance_id: "ob-1", as_of_date: "2026-07-01", outstanding: "26510.00",
    },
    {
      credit_customer_id: CUSTOMER_2, name: "Sandhu Dairy", is_active: true, opening_balance: null,
      opening_balance_id: null, as_of_date: null, outstanding: "4200.00",
    },
    {
      credit_customer_id: CUSTOMER_3, name: "Old Mill Traders", is_active: false, opening_balance: "0.00",
      opening_balance_id: "ob-3", as_of_date: "2026-07-01", outstanding: "0.00",
    },
  ],
};

export const customerDetail: Schemas["CreditCustomerResponse"] = {
  id: CUSTOMER_ID, name: "Bhullar Transport", phone: "9876543210", vehicle_numbers: ["PB65AX1204"],
  credit_limit: null, is_active: true, outstanding: "26510.00",
};

export const customerLedger: Schemas["app__api__v1__credit_customers__LedgerPage"] = {
  opening_balance: "12400.00",
  outstanding: "26510.00",
  truncated: false,
  items: [
    { id: "lg-3", kind: "repayment", amount: "5000.00", balance_delta: "-5000.00", balance_after: "26510.00", business_date: BUSINESS_DATE, created_at: "2026-10-02T11:40:00Z", is_reversal: false, shift_id: SHIFT_ID },
    { id: "lg-2", kind: "sale", amount: "19110.00", balance_delta: "19110.00", balance_after: "31510.00", business_date: BUSINESS_DATE, created_at: "2026-10-02T09:15:00Z", is_reversal: false, shift_id: SHIFT_ID },
    { id: "lg-1", kind: "opening", amount: "12400.00", balance_delta: "12400.00", balance_after: "12400.00", business_date: "2026-07-01", created_at: "2026-08-29T10:00:00Z", is_reversal: false, shift_id: null },
  ],
};

export const datedRepayments: Schemas["DatedCreditRepaymentPage"] = {
  next_cursor: null,
  items: [
    {
      id: "dr-1", shift_id: null, business_date: "2026-09-29", credit_customer_id: CUSTOMER_2, mode: "bank_transfer",
      amount: "10000.00", attachment_id: null, reverses_id: null, reversal_reason: null, is_reversed: false,
      created_at: "2026-09-30T08:00:00Z",
    },
  ],
};

function statementRow(over: Partial<Schemas["StatementRowResponse"]>): Schemas["StatementRowResponse"] {
  return {
    customer_id: CUSTOMER_ID, name: "Bhullar Transport", phone: "9876543210", is_active: true, opening_balance_entered: true,
    owed_before: "0.00", udhaar_in: "0.00", repaid_in: "0.00", billed: "0.00", udhaar_since: "0.00", opening_since: "0.00",
    paid_since: "0.00", owes_today: "0.00", lines: [], ...over,
  };
}

export const statement: Schemas["StatementResponse"] = {
  from: "2026-09-16",
  to: "2026-09-30",
  today: BUSINESS_DATE,
  open_shift_count: 0,
  lines_truncated: false,
  rows: [
    statementRow({
      owed_before: "12400.00", udhaar_in: "8400.00", repaid_in: "0.00", billed: "20800.00",
      udhaar_since: "19110.00", paid_since: "5000.00", owes_today: "34910.00",
      lines: [
        {
          id: "sl-1", kind: "sale", period: "in_range", business_date: "2026-09-22", amount: "8400.00", fuel_display_name: "Diesel",
          quantity: "93.640", unit_of_measure: "litre", vehicle_number: "PB65AX1204", mode: null, shift_id: "shift-sep22",
          bank_reference: null, bank_status: null, is_reversal: false, is_reversed: false, reversal_reason: null,
        },
        {
          id: "sl-2", kind: "repayment", period: "since", business_date: BUSINESS_DATE, amount: "5000.00", fuel_display_name: null,
          quantity: null, unit_of_measure: null, vehicle_number: null, mode: "cash", shift_id: SHIFT_ID,
          bank_reference: null, bank_status: null, is_reversal: false, is_reversed: false, reversal_reason: null,
        },
      ],
    }),
    // Never entered and nothing before the window: ₹0.00 by arithmetic, "not entered" in fact.
    statementRow({
      customer_id: CUSTOMER_2, name: "Sandhu Dairy", phone: "9814000000", opening_balance_entered: false,
      udhaar_in: "14200.00", repaid_in: "10000.00", billed: "4200.00", owes_today: "4200.00",
      lines: [
        {
          id: "sl-3", kind: "repayment", period: "in_range", business_date: "2026-09-29", amount: "10000.00", fuel_display_name: null,
          quantity: null, unit_of_measure: null, vehicle_number: null, mode: "bank_transfer", shift_id: null,
          bank_reference: "UTR 4417", bank_status: "verified", is_reversal: false, is_reversed: false, reversal_reason: null,
        },
      ],
    }),
  ],
  totals: {
    owed_before: "12400.00", udhaar_in: "22600.00", repaid_in: "10000.00", billed: "25000.00",
    udhaar_since: "19110.00", opening_since: "0.00", paid_since: "5000.00", owes_today: "39110.00",
  },
};

export const BANK_ACCOUNT_ID = "ba000000-0000-4000-8000-000000000401";

export const bankAccounts: Schemas["BankAccountResponse"][] = [
  { id: BANK_ACCOUNT_ID, label: "BoB Current", bank_name: "Bank of Baroda", account_number_last4: "4471", is_active: true },
];

export const imports: Schemas["ImportPage"] = {
  next_cursor: null,
  items: [
    {
      id: "im-1", bank_account_id: BANK_ACCOUNT_ID, period_from: "2026-09-01", period_to: "2026-10-01", original_filename: "OpTransactionHistory.csv",
      row_count: 61, imported_count: 61, skipped_count: 0, opening_balance: "184220.50", closing_balance: "212904.17",
    },
  ],
};

function line(over: Partial<Schemas["TransactionResponse"]>): Schemas["TransactionResponse"] {
  return {
    id: "tx", txn_date: "2026-09-29", narration: "", amount: "0.00", direction: "credit", running_balance: null,
    classification: "udhaar_repayment", is_expense: "undecided", suggested_expense: "undecided",
    matched_business_date: null, credit_repayment_id: null, ...over,
  };
}

/** Three incoming lines, one of each kind the review must tell apart: one already typed in by
 * hand (verified), one a remembered sender proposes, one that matches two typed-in payments. */
export const creditLines: Schemas["TransactionPage"] = {
  next_cursor: null,
  items: [
    line({ id: "tx-verified", txn_date: "2026-09-29", narration: "NEFT UTR 4417 SANDHU DAIRY", amount: "10000.00" }),
    line({ id: "tx-proposed", txn_date: "2026-09-27", narration: "UPI/BHULLAR TPT/PAYMENT", amount: "7500.00" }),
    line({ id: "tx-ambiguous", txn_date: "2026-09-24", narration: "IMPS 99812 TRANSFER", amount: "2000.00" }),
  ],
};

export const debitLines: Schemas["TransactionPage"] = {
  next_cursor: null,
  items: [
    line({ id: "tx-iocl", direction: "debit", narration: "INDIAN OIL CORPORATION LTD", amount: "500000.00", classification: "iocl_ms_hsd", suggested_expense: "no" }),
    line({ id: "tx-charge", direction: "debit", narration: "Charges for PORD", amount: "59.00", classification: "bank_charge", suggested_expense: "yes" }),
  ],
};

export const reconciliation: Schemas["ReconciliationResponse"] = {
  date_from: "2026-09-01",
  date_to: "2026-10-01",
  boundary_settlement: "48211.30",
  boundary_settled_on: "2026-10-02",
  settlements: [
    { business_date: "2026-09-29", settled_on: "2026-09-30", expected: "265617.00", settled: "265617.00", difference: "0.00", matches: true, source: "snapshot" },
    { business_date: "2026-09-30", settled_on: "2026-10-01", expected: "251004.00", settled: "250504.00", difference: "-500.00", matches: false, source: "computed" },
    { business_date: "2026-10-01", settled_on: "2026-10-02", expected: "48211.30", settled: null, difference: null, matches: false, source: "computed" },
  ],
  deposits: [
    { kind: "matched", txn_date: "2026-09-22", amount: "60000.00", transaction_id: "tx-d1", bank_deposit_id: "bd-1", days_late: 1 },
    { kind: "missing_from_bank", txn_date: "2026-09-26", amount: "40000.00", transaction_id: null, bank_deposit_id: "bd-2", days_late: null },
  ],
  credits: [
    { transaction_id: "tx-verified", txn_date: "2026-09-29", narration: "NEFT UTR 4417 SANDHU DAIRY", amount: "10000.00", verified_repayment_id: "dr-1", ambiguous: false, proposals: [] },
    {
      transaction_id: "tx-proposed", txn_date: "2026-09-27", narration: "UPI/BHULLAR TPT/PAYMENT", amount: "7500.00", verified_repayment_id: null, ambiguous: false,
      proposals: [{ credit_customer_id: CUSTOMER_ID, name: "Bhullar Transport", reason: "Remembered sender “BHULLAR TPT”", confidence: "high" }],
    },
    { transaction_id: "tx-ambiguous", txn_date: "2026-09-24", narration: "IMPS 99812 TRANSFER", amount: "2000.00", verified_repayment_id: null, ambiguous: true, proposals: [] },
  ],
};

/* --- the Summary tab ---------------------------------------------------------------------- */

function trendDay(date: string, total: string | null, height: string, source: Schemas["SummaryTrendDayResponse"]["source"] = "computed"): Schemas["SummaryTrendDayResponse"] {
  return { business_date: date, total_sales: total, bar_height_pct: height, source, alert: false };
}

/** Eleven days, so the bars page; CBG has no commission, so the combined margin is withheld. */
export const summaryReport: Schemas["app__api__v1__reports__SummaryResponse"] = {
  from: "2026-09-22",
  to: BUSINESS_DATE,
  window_basis: "Days with a finalised summary are read from it; every other trading day is calculated live.",
  fuel_basis: "Each shift valued at the rate effective at its start.",
  profit_basis: "Quantity sold × dealer commission. Not business profit.",
  cash_basis: "Cash is derived, not declared.",
  trading_days: 10,
  partial: false,
  days_by_source: { snapshot: 8, computed: 2, no_trading: 1, unavailable: 0 },
  total_sales: "2914306.40",
  metered_fuel_sales: "2903806.40",
  non_fuel_sales_total: "10500.00",
  fuel_sales_total: "2903806.40",
  gross_fuel_margin_total: null,
  fuels_missing_margin: ["CBG"],
  fuel: [
    {
      fuel_type_id: "ft-petrol", code: "PETROL", display_name: "Petrol", unit_of_measure: "litre", quantity: "17120.400",
      sale_value: "1777195.92", share_pct: "61.20%", rate_per_unit: null, margin_per_unit: "3.99", gross_fuel_margin: "68310.40", margin_unavailable_reason: null,
    },
    {
      fuel_type_id: "ft-diesel", code: "DIESEL", display_name: "Diesel", unit_of_measure: "litre", quantity: "11014.800",
      sale_value: "985335.28", share_pct: "33.93%", rate_per_unit: null, margin_per_unit: "2.57", gross_fuel_margin: "28308.04", margin_unavailable_reason: null,
    },
    {
      fuel_type_id: "ft-cbg", code: "CBG", display_name: "CBG", unit_of_measure: "kilogram", quantity: "1550.250",
      sale_value: "141275.20", share_pct: "4.87%", rate_per_unit: null, margin_per_unit: null, gross_fuel_margin: null, margin_unavailable_reason: "NO_MARGIN_FOR_DATE",
    },
  ],
  quantity_by_unit: { litre: "28135.200", kilogram: "1550.250" },
  card_total: "1404220.00",
  upi_total: "1003610.15",
  wallet_total: "0.00",
  cash_sales: "306086.25",
  credit_sales_total: "200390.00",
  cash_credit_repayments: "42000.00",
  card_upi_credit_repayments: "15000.00",
  expenses_total: "88420.00",
  // Largest first, with `bar_pct` scaled to the largest (Phase 26).
  expenses_by_category: [
    { code: "SALARY", display_name: "Salaries", amount: "60000.00", share_pct: "67.86%", bar_pct: "100.00%" },
    { code: "ELECTRICITY", display_name: "Electricity", amount: "21870.00", share_pct: "24.73%", bar_pct: "36.45%" },
    { code: "MAINTENANCE", display_name: "Maintenance", amount: "6550.00", share_pct: "7.41%", bar_pct: "10.92%" },
  ],
  bank_deposits_total: "250000.00",
  shortfalls_booked: "500.00",
  // 4,12,850 + 2,00,390 − 1,48,720.50 = 4,64,519.50, over an extent of 6,13,240.
  credit: {
    owed_at_start: "412850.00",
    given: "200390.00",
    collected: "148720.50",
    owed_at_end: "464519.50",
    owes_today: "471019.50",
    customers_owing: 14,
    bridge: [
      { key: "start", amount: "412850.00", offset_pct: "0.00%", width_pct: "67.32%" },
      { key: "given", amount: "200390.00", offset_pct: "67.32%", width_pct: "32.68%" },
      { key: "collected", amount: "148720.50", offset_pct: "75.75%", width_pct: "24.25%" },
      { key: "end", amount: "464519.50", offset_pct: "0.00%", width_pct: "75.75%" },
    ],
    top_owing: [
      { customer_id: CUSTOMER_ID, name: "Ramesh Transport", owed_at_end: "96400.00" },
      { customer_id: "c0ffee00-0000-4000-8000-000000000202", name: "Gupta Tractors", owed_at_end: "71250.00" },
      { customer_id: "c0ffee00-0000-4000-8000-000000000203", name: "Verma Logistics", owed_at_end: "58900.50" },
      { customer_id: "c0ffee00-0000-4000-8000-000000000204", name: "Singh Roadways", owed_at_end: "44120.00" },
      { customer_id: "c0ffee00-0000-4000-8000-000000000205", name: "Bansal Agro", owed_at_end: "31775.00" },
    ],
    basis: "The billing statement's totals for the same dates, summed over every customer.",
  },
  trend: [
    trendDay("2026-09-22", "281220.10", "88.20%", "snapshot"),
    trendDay("2026-09-23", "297410.00", "93.27%", "snapshot"),
    trendDay("2026-09-24", "264118.55", "82.83%", "snapshot"),
    trendDay("2026-09-25", null, "0.00%", "no_trading"),
    trendDay("2026-09-26", "301877.20", "94.67%", "snapshot"),
    trendDay("2026-09-27", "318866.00", "100.00%", "snapshot"),
    trendDay("2026-09-28", "289004.75", "90.63%", "snapshot"),
    trendDay("2026-09-29", "276540.00", "86.73%", "snapshot"),
    trendDay("2026-09-30", "292611.35", "91.77%", "snapshot"),
    trendDay(PREVIOUS_DATE, "289831.00", "90.89%"),
    trendDay(BUSINESS_DATE, "302827.45", "94.97%"),
  ],
};

/* The list behind each expense bar (Phase 26). Each total equals its bar's amount above, as the
 * server guarantees by feeding both from one query. Maintenance carries a reversal pair, which
 * nets out of its day. */
function drillItem(
  id: string,
  amount: string,
  description: string,
  extra: Partial<Schemas["ExpenseDrillRowResponse"]> = {},
): Schemas["ExpenseDrillRowResponse"] {
  return {
    id: `e0e0e0e0-0000-4000-8000-${id.padStart(12, "0")}`,
    shift_id: "5f5f5f5f-0000-4000-8000-000000000001",
    mode: "cash",
    amount,
    description,
    paid_to: null,
    is_reversal: false,
    is_reversed: false,
    reversal_reason: null,
    ...extra,
  };
}

const drillWindow = { from: "2026-09-22", to: BUSINESS_DATE };

export const expenseDrill: Record<string, Schemas["ExpenseDrillResponse"]> = {
  SALARY: {
    ...drillWindow,
    code: "SALARY",
    display_name: "Salaries",
    total: "60000.00",
    row_count: 3,
    truncated: false,
    days: [
      {
        business_date: PREVIOUS_DATE,
        total: "60000.00",
        items: [
          drillItem("1", "20000.00", "September salary", { paid_to: "Ramesh", mode: "bank_transfer" }),
          drillItem("2", "20000.00", "September salary", { paid_to: "Suresh", mode: "bank_transfer" }),
          drillItem("3", "20000.00", "September salary", { paid_to: "Mohan", mode: "cash" }),
        ],
      },
    ],
  },
  ELECTRICITY: {
    ...drillWindow,
    code: "ELECTRICITY",
    display_name: "Electricity",
    total: "21870.00",
    row_count: 1,
    truncated: false,
    days: [
      {
        business_date: "2026-09-28",
        total: "21870.00",
        items: [drillItem("4", "21870.00", "PSPCL bill, September", { paid_to: "PSPCL", mode: "bank_transfer" })],
      },
    ],
  },
  MAINTENANCE: {
    ...drillWindow,
    code: "MAINTENANCE",
    display_name: "Maintenance",
    total: "6550.00",
    row_count: 5,
    truncated: false,
    days: [
      {
        business_date: BUSINESS_DATE,
        total: "3250.00",
        items: [
          drillItem("5", "2800.00", "Nozzle seal replaced", { paid_to: "Sharma Pumps" }),
          drillItem("6", "450.00", "Hose clamp"),
        ],
      },
      {
        business_date: "2026-09-26",
        total: "3300.00",
        items: [
          drillItem("7", "3300.00", "Canopy light fitting", { paid_to: "Jain Electricals", mode: "upi" }),
          drillItem("8", "900.00", "Canopy light fitting", { is_reversed: true }),
          drillItem("9", "-900.00", "Canopy light fitting", { is_reversal: true, reversal_reason: "Entered twice" }),
        ],
      },
    ],
  },
};

/* --- the Admin tab ------------------------------------------------------------------------ */

export const nozzles: Schemas["NozzleResponse"][] = [
  {
    id: NOZZLE_ID, outlet_id: OUTLET_ID, label: "DU-1/N-1", dispenser_label: "DU-1", fuel_type_id: "ft-1", fuel_type_code: "PETROL",
    unit_of_measure: "litre", totalizer_max_value: "999999.99", meter_installed_at: "2025-04-01T04:30:00Z", is_active: true,
  },
  {
    id: "b0b0b0b0-0000-4000-8000-000000000302", outlet_id: OUTLET_ID, label: "CBG-1", dispenser_label: "CBG", fuel_type_id: "ft-2", fuel_type_code: "CBG",
    unit_of_measure: "kilogram", totalizer_max_value: "99999.99", meter_installed_at: "2025-06-10T04:30:00Z", is_active: false,
  },
];

export const pricesCurrent: Schemas["CurrentRateResponse"][] = [
  { fuel_type_id: "ft-1", fuel_type_code: "PETROL", rate_per_unit: "103.81", at: "2026-10-03T06:30:00Z" },
  { fuel_type_id: "ft-2", fuel_type_code: "CBG", rate_per_unit: "91.13", at: "2026-10-03T06:30:00Z" },
];

export const pricesHistory: Schemas["FuelPricePage"] = {
  next_cursor: null,
  items: [
    { id: "fp-2", outlet_id: OUTLET_ID, fuel_type_id: "ft-1", rate_per_unit: "103.81", effective_from: "2026-09-15T00:30:00Z", entered_by: ME_ID, is_backdated: true },
    { id: "fp-1", outlet_id: OUTLET_ID, fuel_type_id: "ft-1", rate_per_unit: "103.54", effective_from: "2026-06-29T00:30:00Z", entered_by: ME_ID, is_backdated: false },
  ],
};

/** Petrol only: CBG has a rate and no margin, so the screen must name it as missing. */
export const marginsCurrent: Schemas["CurrentMarginResponse"][] = [
  { fuel_type_id: "ft-1", fuel_type_code: "PETROL", margin_per_unit: "3.99", at: "2026-10-03T06:30:00Z" },
];

export const marginsHistory: Schemas["FuelMarginPage"] = {
  next_cursor: null,
  items: [{ id: "fm-1", outlet_id: OUTLET_ID, fuel_type_id: "ft-1", margin_per_unit: "3.99", effective_from: "2026-06-29T00:30:00Z", entered_by: ME_ID, is_backdated: false }],
};

export const shiftTemplates: Schemas["ShiftTemplateResponse"][] = [
  { id: "tpl-1", outlet_id: OUTLET_ID, sequence: 1, label: "Day", starts_at_local: "06:00:00", ends_at_local: "22:00:00", crosses_midnight: false, is_active: true },
];

export const outstandingCustomers: Schemas["CreditCustomerResponse"][] = [
  { ...customerDetail },
  { id: CUSTOMER_2, name: "Sandhu Dairy", phone: "9814000000", vehicle_numbers: null, credit_limit: "50000.00", is_active: true, outstanding: "-1200.00" },
];

export const users: Schemas["UserListItem"][] = [
  { id: ME_ID, full_name: "Harjit Kaur", role: "admin", is_active: true },
  { id: SALESMAN_ID, full_name: "Gurpreet Singh", role: "attendant", is_active: true },
];

export const salesmanDetail: Schemas["UserResponse"] = {
  id: SALESMAN_ID, full_name: "Gurpreet Singh", phone: "9876500000", role: "attendant", is_active: true, profile_is_active: true,
  created_at: "2026-07-01T05:00:00Z",
};

export const auditPage: Schemas["AuditLogPage"] = {
  next_cursor: null,
  items: [
    {
      id: "al-2", table_name: "credit_customers", record_id: CUSTOMER_2, action: "update", changed_by: ME_ID, changed_at: "2026-10-02T12:05:00Z",
      request_id: "req-2", old_values: { name: "Sandhu Dairy", credit_limit: null }, new_values: { name: "Sandhu Dairy", credit_limit: "50000.00" },
    },
    {
      id: "al-1", table_name: "fuel_prices", record_id: "fp-2", action: "insert", changed_by: ME_ID, changed_at: "2026-10-01T09:00:00Z",
      request_id: "req-1", old_values: null, new_values: { fuel_type_id: "ft-1", rate_per_unit: "103.81", effective_from: "2026-09-15T00:30:00+00:00" },
    },
  ],
};
