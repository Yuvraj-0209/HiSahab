/* Every figure on the front door (Phase 24 D7, CLAUDE.md §14).
 *
 * **These are sample figures, and the page says so.** They are literal strings, written by hand:
 * nothing here is fetched, nothing is computed in JavaScript, and every name is invented. A demo
 * that added rupees in the browser would be §3 rule 1 one language further out, and a real
 * customer's balance on a public page would be a leak.
 *
 * They are also chosen to *agree with each other*, because a buyer who checks the arithmetic
 * should find it right, and so should anyone reading this file:
 *
 *   the gap      1,24,560.00 − 38,210.00 − 51,940.00 − 6,400.00 = 28,010.00 accountable;
 *                28,010.00 declared as 27,510.00 is 500.00 short
 *   the ledger   12,400.00 + 3,200.00 = 15,600.00; − 10,000.00 = 5,600.00
 *   the bank     yesterday's card 38,210.00 + UPI 51,940.00 = 90,150.00 from Paytm
 */

export const METER = {
  nozzle: "DU-1 / N-2",
  fuel: "Petrol",
  carried: "61,822.10",
  meterAgrees: "61,822.10",
  meterDisagrees: "61,839.40",
  moved: "17.30 L moved between shifts",
};

export const GAP = {
  rows: [
    { label: "Metered fuel sales", value: "₹1,24,560.00", sign: "" },
    { label: "Card", value: "₹38,210.00", sign: "−" },
    { label: "UPI", value: "₹51,940.00", sign: "−" },
    { label: "Udhaar given", value: "₹6,400.00", sign: "−" },
  ],
  accountable: "₹28,010.00",
  declared: "₹27,510.00",
  gap: "₹500.00",
};

export const LEDGER = {
  customer: "Ramesh Transport",
  rows: [
    { date: "1 Sep", label: "Owed when the books began", amount: "₹12,400.00", balance: "₹12,400.00" },
    { date: "5 Sep", label: "Udhaar, MH-12 diesel", amount: "₹3,200.00", balance: "₹15,600.00" },
    { date: "12 Sep", label: "Paid by bank transfer", amount: "−₹10,000.00", balance: "₹5,600.00" },
  ],
  bill: { period: "1 to 15 September", before: "₹12,400.00", udhaar: "₹3,200.00", repaid: "₹10,000.00", billed: "₹5,600.00" },
};

export const BANK = [
  { narration: "PAYTM PAYMENTS SERVICES", amount: "₹90,150.00", direction: "credit", verdict: "Matches yesterday's card and UPI" },
  { narration: "BY CASH", amount: "₹25,000.00", direction: "credit", verdict: "Matches the deposit entered on 14 Sep" },
  { narration: "NEFT RAMESH TRANSPORT", amount: "₹10,000.00", direction: "credit", verdict: "Matches Ramesh Transport's repayment" },
  { narration: "INDIAN OIL CORPORATION", amount: "₹4,00,000.00", direction: "debit", verdict: "An IOCL top-up. Never an expense" },
] as const;

/** Ten days of sales bars. `bar_height_pct` is a literal percentage, as the server would send. */
export const DAYS = [
  ["2026-09-01", "₹1,18,240.00", "78%"],
  ["2026-09-02", "₹1,26,905.00", "84%"],
  ["2026-09-03", "₹1,09,330.00", "72%"],
  ["2026-09-04", "₹1,31,780.00", "87%"],
  ["2026-09-05", "₹1,51,260.00", "100%"],
  ["2026-09-06", "₹1,44,915.00", "96%"],
  ["2026-09-07", "₹1,12,470.00", "74%"],
  ["2026-09-08", "₹1,20,635.00", "80%"],
  ["2026-09-09", "₹1,24,560.00", "82%"],
  ["2026-09-10", "₹1,29,090.00", "85%"],
].map(([business_date = "", total_sales = "", bar_height_pct = ""]) => ({
  business_date,
  total_sales,
  bar_height_pct,
  source: "snapshot" as const,
  alert: false,
}));

export const FUEL_MIX = [
  { key: "Petrol", share_pct: "46.2%", colour: 1, value: "₹5,71,330.00" },
  { key: "Diesel", share_pct: "41.5%", colour: 2, value: "₹5,13,215.00" },
  { key: "CBG", share_pct: "12.3%", colour: 3, value: "₹1,52,110.00" },
];

export const PAYMENT_MIX = [
  { key: "upi", label: "UPI", value: "₹5,02,860.00", share_pct: "40.7%", colour: 1 },
  { key: "card", label: "Card", value: "₹3,71,480.00", share_pct: "30.0%", colour: 2 },
  { key: "cash", label: "Cash", value: "₹2,99,120.00", share_pct: "24.2%", colour: 3 },
  { key: "udhaar", label: "Udhaar", value: "₹63,195.00", share_pct: "5.1%", colour: 5 },
];

export const MONTH = { sales: "₹12,36,655.00", margin: "₹41,382.50" };
