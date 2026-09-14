"""The bank statement endpoints (CLAUDE.md §5.3a, §8, §9, §14).

Nine routes, one of which writes money. These tests spend most of their effort on that one --
`confirm-repayments` -- and on proving the other eight write nothing at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

pytestmark = pytest.mark.asyncio

_CSV_HEADER = (
    "TRAN DATE,NARRATION,WITHDRAWAL(DR),DEPOSIT(CR),BALANCE(INR)\n"
)


def _csv(*rows: str) -> bytes:
    return (_CSV_HEADER + "".join(rows)).encode()


async def _make_account(client, headers, *, label: str = "BoB Current") -> UUID:
    response = await client.post(
        "/api/v1/bank-accounts",
        json={"label": label, "bank_name": "Bank of Baroda", "account_number_last4": "0089"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def _import(client, headers, account_id: UUID, data: bytes, *, key: str | None = None):
    return await client.post(
        "/api/v1/bank-statements/imports",
        files={"file": ("july.csv", data, "text/csv")},
        data={"bank_account_id": str(account_id)},
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )


class TestBankAccounts:
    async def test_an_admin_creates_and_lists_an_account(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        account_id = await _make_account(client, auth_headers(admin))

        listed = await client.get("/api/v1/bank-accounts", headers=auth_headers(admin))

        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [str(account_id)]

    async def test_a_manager_may_read_but_not_create(
        self, client, make_user, auth_headers
    ) -> None:
        """§8: managing accounts is admin, like every other reference table."""
        manager = make_user("manager")

        created = await client.post(
            "/api/v1/bank-accounts",
            json={"label": "Nope", "bank_name": "Bank"},
            headers=auth_headers(manager),
        )
        listed = await client.get("/api/v1/bank-accounts", headers=auth_headers(manager))

        assert created.status_code == 403
        assert listed.status_code == 200

    async def test_an_attendant_is_refused_entirely(
        self, client, make_user, auth_headers
    ) -> None:
        """§8 has never let an attendant see a customer's balance, and a statement is a
        month of them."""
        attendant = make_user("attendant")

        response = await client.get("/api/v1/bank-accounts", headers=auth_headers(attendant))

        assert response.status_code == 403

    async def test_a_duplicate_label_is_a_named_conflict(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        await _make_account(client, auth_headers(admin), label="Same")

        response = await client.post(
            "/api/v1/bank-accounts",
            json={"label": "Same", "bank_name": "Other Bank"},
            headers=auth_headers(admin),
        )

        assert response.status_code == 409
        assert response.json()["code"] == "BANK_ACCOUNT_LABEL_EXISTS"

    async def test_only_the_last_four_digits_are_accepted(
        self, client, make_user, auth_headers
    ) -> None:
        """A full account number reconciles nothing and makes the table worth stealing."""
        admin = make_user("admin")

        response = await client.post(
            "/api/v1/bank-accounts",
            json={
                "label": "Full number",
                "bank_name": "Bank",
                "account_number_last4": "38100600001500",
            },
            headers=auth_headers(admin),
        )

        assert response.status_code == 422


class TestImporting:
    async def test_a_statement_imports_and_classifies(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)

        response = await _import(
            client,
            headers,
            account_id,
            _csv(
                "02/07/2026,RTGS-YESBR1-PAYTM PAYMENTS SERVICE,,5000.00, 16000.00Cr\n",
                "01/07/2026,BY CASH,,1000.00, 11000.00Cr\n",
            ),
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["row_count"] == 2
        assert body["imported_count"] == 2
        assert body["skipped_count"] == 0
        assert body["period_from"] == "2026-07-01"
        assert body["period_to"] == "2026-07-02"

    async def test_reuploading_the_same_file_imports_nothing(
        self, client, make_user, auth_headers
    ) -> None:
        """**Re-uploading an overlapping period is the normal way to work**, not an error.

        The T+1 boundary means the owner downloads with a few days' overlap on purpose, so
        the second upload of a month must be harmless rather than merely refused.
        """
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        data = _csv("01/07/2026,BY CASH,,1000.00, 11000.00Cr\n")

        first = await _import(client, headers, account_id, data)
        second = await _import(client, headers, account_id, data)

        assert first.json()["imported_count"] == 1
        assert second.json()["imported_count"] == 0
        assert second.json()["skipped_count"] == 1

    async def test_an_overlapping_range_imports_only_the_new_lines(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)

        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,BY CASH,,1000.00, 11000.00Cr\n"),
        )
        second = await _import(
            client,
            headers,
            account_id,
            _csv(
                "02/07/2026,NEFT-SOMEBODY,,2000.00, 13000.00Cr\n",
                "01/07/2026,BY CASH,,1000.00, 11000.00Cr\n",
            ),
        )

        assert second.json()["imported_count"] == 1
        assert second.json()["skipped_count"] == 1

    async def test_an_unrecognised_format_is_refused_and_writes_nothing(
        self, client, make_user, auth_headers, engine
    ) -> None:
        """§13.36: the parser refuses rather than guessing, and the refusal leaves no trace."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)

        with engine.connect() as connection:
            before = connection.execute(
                text("SELECT count(*) FROM bank_statement_imports")
            ).scalar_one()

        response = await _import(
            client, headers, account_id, b"Date,Details,Amount\n01/07/2026,X,100\n"
        )

        with engine.connect() as connection:
            after = connection.execute(
                text("SELECT count(*) FROM bank_statement_imports")
            ).scalar_one()

        assert response.status_code == 422
        assert response.json()["code"] == "UNRECOGNISED_STATEMENT_FORMAT"
        assert after == before

    async def test_an_import_requires_an_idempotency_key(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)

        response = await client.post(
            "/api/v1/bank-statements/imports",
            files={"file": ("july.csv", _csv("01/07/2026,BY CASH,,1000.00, 1000.00Cr\n"), "text/csv")},
            data={"bank_account_id": str(account_id)},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    async def test_a_replayed_key_returns_the_first_answer(
        self, client, make_user, auth_headers
    ) -> None:
        """§6.10: a timed-out retry must not leave a second import row claiming zero lines."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        data = _csv("01/07/2026,BY CASH,,1000.00, 11000.00Cr\n")
        key = str(uuid4())

        first = await _import(client, headers, account_id, data, key=key)
        second = await _import(client, headers, account_id, data, key=key)

        assert first.json() == second.json()

    async def test_an_account_from_another_outlet_is_a_404(
        self, client, make_user, auth_headers, engine
    ) -> None:
        """Cross-outlet is 404, never 403 -- existence is not leaked across tenants (§7.3)."""
        admin = make_user("admin")
        other_outlet = uuid4()
        other_account = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO outlets (id, name) VALUES (:id, 'Elsewhere')").bindparams(
                    id=other_outlet
                )
            )
            connection.execute(
                text(
                    "INSERT INTO bank_accounts (id, outlet_id, label, bank_name) "
                    "VALUES (:id, :outlet, 'Theirs', 'Bank')"
                ).bindparams(id=other_account, outlet=other_outlet)
            )

        try:
            response = await _import(
                client,
                auth_headers(admin),
                other_account,
                _csv("01/07/2026,BY CASH,,1000.00, 1000.00Cr\n"),
            )

            assert response.status_code == 404
        finally:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM bank_accounts WHERE id = :id").bindparams(
                        id=other_account
                    )
                )
                connection.execute(
                    text("DELETE FROM outlets WHERE id = :id").bindparams(id=other_outlet)
                )


class TestClassificationEdits:
    async def test_a_manager_may_correct_a_classification(
        self, client, make_user, auth_headers
    ) -> None:
        """An ordinary UPDATE, and §6.9 is not breached: correcting our *opinion* of a line
        moves no money."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,EBANK:SOMETHING ODD,500.00,, 500.00Cr\n"),
        )
        listed = await client.get("/api/v1/bank-transactions", headers=headers)
        txn_id = listed.json()["items"][0]["id"]

        response = await client.patch(
            f"/api/v1/bank-transactions/{txn_id}",
            json={"classification": "self_transfer"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["classification"] == "self_transfer"

    async def test_a_credit_cannot_be_marked_an_expense(
        self, client, make_user, auth_headers
    ) -> None:
        """Incoming money counted as a cost is wrong by twice its value in Phase 21."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,NEFT-SOMEBODY,,1000.00, 1000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]

        response = await client.patch(
            f"/api/v1/bank-transactions/{txn_id}",
            json={"is_expense": "yes"},
            headers=headers,
        )

        assert response.status_code == 422
        assert response.json()["code"] == "ONLY_DEBITS_ARE_EXPENSES"

    async def test_a_debit_defaults_to_undecided_and_carries_a_suggestion(
        self, client, make_user, auth_headers
    ) -> None:
        """§6.8's rule: the stored value says "nobody has looked", the suggestion is advice."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,Charges for PORD Customer Payment,58.00,, 942.00Cr\n"),
        )

        row = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]

        assert row["classification"] == "bank_charge"
        assert row["is_expense"] == "undecided"
        assert row["suggested_expense"] == "yes"


class TestConfirmingRepayments:
    """The one endpoint that writes money."""

    async def test_confirming_creates_a_shiftless_bank_transfer_repayment(
        self, client, make_user, auth_headers, make_credit_customer, engine
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gupta Overseas")
        await _import(
            client,
            headers,
            account_id,
            _csv("16/07/2026,NEFT-HDFCH01-GUPTA OVERSEAS,,34599.00, 50000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]

        response = await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={"items": [{"transaction_id": txn_id, "credit_customer_id": str(customer)}]},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        assert response.status_code == 201, response.text
        assert len(response.json()["created"]) == 1

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT shift_id, mode, amount, business_date FROM credit_repayments "
                    "WHERE credit_customer_id = :customer"
                ).bindparams(customer=customer)
            ).one()

        assert row.shift_id is None
        assert row.mode == "bank_transfer"
        assert row.amount == Decimal("34599.00")
        assert row.business_date == date(2026, 7, 16)

    async def test_the_customers_outstanding_moves_by_exactly_that_amount(
        self, client, make_user, auth_headers, make_credit_customer, make_credit_sale,
        make_shift, make_attachment
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gupta Overseas")
        shift = make_shift(business_date=date(2026, 7, 15), attendant_id=admin)
        make_credit_sale(
            shift, customer, amount="50000.00", attachment_id=make_attachment(admin)
        )

        before = await client.get(
            f"/api/v1/credit-customers/{customer}", headers=headers
        )
        await _import(
            client,
            headers,
            account_id,
            _csv("16/07/2026,NEFT-HDFCH01-GUPTA OVERSEAS,,20000.00, 50000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]
        await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={"items": [{"transaction_id": txn_id, "credit_customer_id": str(customer)}]},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        after = await client.get(f"/api/v1/credit-customers/{customer}", headers=headers)

        assert Decimal(before.json()["outstanding"]) == Decimal("50000.00")
        assert Decimal(after.json()["outstanding"]) == Decimal("30000.00")

    async def test_a_line_cannot_be_confirmed_twice(
        self, client, make_user, auth_headers, make_credit_customer
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gupta Overseas")
        await _import(
            client,
            headers,
            account_id,
            _csv("16/07/2026,NEFT-GUPTA OVERSEAS,,1000.00, 1000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]
        body = {"items": [{"transaction_id": txn_id, "credit_customer_id": str(customer)}]}

        await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json=body,
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        second = await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json=body,
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        assert second.json()["created"] == []
        assert second.json()["failed"][0]["code"] == "ALREADY_CONFIRMED"

    async def test_a_partial_batch_lands_the_good_rows_and_names_the_bad(
        self, client, make_user, auth_headers, make_credit_customer
    ) -> None:
        """**Refusing the whole batch for one bad line would leave no way to proceed.**

        That is the friction §6.8 warns teaches people to work around the system -- here, by
        confirming forty lines one at a time until they find the broken one.
        """
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gupta Overseas")
        await _import(
            client,
            headers,
            account_id,
            _csv(
                "17/07/2026,NEFT-GUPTA OVERSEAS,,2000.00, 3000.00Cr\n",
                "16/07/2026,NEFT-GUPTA OVERSEAS,,1000.00, 1000.00Cr\n",
            ),
        )
        rows = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ]

        response = await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={
                "items": [
                    {
                        "transaction_id": rows[0]["id"],
                        "credit_customer_id": str(customer),
                    },
                    {
                        "transaction_id": str(uuid4()),
                        "credit_customer_id": str(customer),
                    },
                ]
            },
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        body = response.json()
        assert len(body["created"]) == 1
        assert len(body["failed"]) == 1
        assert body["failed"][0]["code"] == "BANK_TRANSACTION_NOT_FOUND"

    async def test_a_deactivated_customer_may_still_repay(
        self, client, make_user, auth_headers, make_credit_customer
    ) -> None:
        """§5.1's asymmetry: you deactivate somebody to stop the debt growing while they pay
        it off. Refusing their money would strand a balance nothing could clear."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gone Away", is_active=False)
        await _import(
            client,
            headers,
            account_id,
            _csv("16/07/2026,NEFT-GONE AWAY,,1000.00, 1000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]

        response = await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={"items": [{"transaction_id": txn_id, "credit_customer_id": str(customer)}]},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        assert len(response.json()["created"]) == 1

    async def test_remembering_a_sender_stores_an_alias(
        self, client, make_user, auth_headers, make_credit_customer, engine
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        customer = make_credit_customer(name="Gupta Overseas")
        await _import(
            client,
            headers,
            account_id,
            _csv("16/07/2026,NEFT-HDFCH01131905980-GUPTA OVERSEAS,,1000.00, 1000.00Cr\n"),
        )
        txn_id = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]["id"]

        await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={
                "items": [
                    {
                        "transaction_id": txn_id,
                        "credit_customer_id": str(customer),
                        "remember_sender": True,
                    }
                ]
            },
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        with engine.connect() as connection:
            fragment = connection.execute(
                text(
                    "SELECT fragment FROM bank_sender_aliases WHERE credit_customer_id = :c"
                ).bindparams(c=customer)
            ).scalar_one()

        assert fragment == "GUPTA OVERSEAS"

    async def test_an_attendant_cannot_confirm(
        self, client, make_user, auth_headers, make_credit_customer
    ) -> None:
        attendant = make_user("attendant")
        customer = make_credit_customer(name="Somebody")

        response = await client.post(
            "/api/v1/bank-transactions/confirm-repayments",
            json={
                "items": [
                    {
                        "transaction_id": str(uuid4()),
                        "credit_customer_id": str(customer),
                    }
                ]
            },
            headers={**auth_headers(attendant), "Idempotency-Key": str(uuid4())},
        )

        assert response.status_code == 403


class TestReconciliationEndpoint:
    async def test_it_writes_nothing(
        self, client, make_user, auth_headers, engine
    ) -> None:
        """§8 requires a report to write nothing, and Phase 15's lesson was that a screen
        which implies otherwise teaches a manager a day is settled when it is not."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,BY CASH,,1000.00, 1000.00Cr\n"),
        )

        with engine.connect() as connection:
            before = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM bank_deposits), "
                    "(SELECT count(*) FROM credit_repayments), "
                    "(SELECT count(*) FROM daily_cash_summaries)"
                )
            ).one()

        response = await client.get(
            "/api/v1/bank-statements/reconciliation",
            params={
                "bank_account_id": str(account_id),
                "date_from": "2026-07-01",
                "date_to": "2026-07-01",
            },
            headers=headers,
        )

        with engine.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM bank_deposits), "
                    "(SELECT count(*) FROM credit_repayments), "
                    "(SELECT count(*) FROM daily_cash_summaries)"
                )
            ).one()

        assert response.status_code == 200
        assert after == before

    async def test_a_backwards_range_is_refused(
        self, client, make_user, auth_headers
    ) -> None:
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)

        response = await client.get(
            "/api/v1/bank-statements/reconciliation",
            params={
                "bank_account_id": str(account_id),
                "date_from": "2026-07-31",
                "date_to": "2026-07-01",
            },
            headers=headers,
        )

        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_DATE_RANGE"

    async def test_money_is_returned_as_strings_never_numbers(
        self, client, make_user, auth_headers
    ) -> None:
        """§14: a JSON number is a float the moment a browser parses it."""
        admin = make_user("admin")
        headers = auth_headers(admin)
        account_id = await _make_account(client, headers)
        await _import(
            client,
            headers,
            account_id,
            _csv("01/07/2026,BY CASH,,1000.00, 1000.00Cr\n"),
        )

        row = (await client.get("/api/v1/bank-transactions", headers=headers)).json()[
            "items"
        ][0]

        assert isinstance(row["amount"], str)
        assert isinstance(row["running_balance"], str)
