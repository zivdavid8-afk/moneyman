"""
Pepper -> Google Sheet sync (Ziv's financial manager).

Runs on GitHub Actions. Reads the last ~85 days of the Pepper business
account and appends NEW completed transactions to the same sheet tab that
Moneyman writes to (_moneyman), with companyId "pepper" so the dashboard
can show the business separately.

Secrets it uses (from the GitHub vault, never printed):
  PEPPER_PHONE, PEPPER_PASSWORD, PEPPER_TOKEN  - the Pepper login
  MONEYMAN_CONFIG                              - only for the Google Sheet access
The log prints only counts, never amounts, names or numbers.
"""
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from israeli_finance_scrapers import create_scraper
from israeli_finance_scrapers.providers.banks import pepper as P

DAYS_BACK = 85  # Pepper refuses long ranges (a full year failed)
WORKSHEET = "_moneyman"


def first(x):
    if isinstance(x, list):
        return x[0] if x else {}
    return x or {}


def find_accounts(x, out):
    if isinstance(x, dict):
        if "accountId" in x:
            out.append(x)
        for v in x.values():
            find_accounts(v, out)
    elif isinstance(x, list):
        for v in x:
            find_accounts(v, out)
    return out


def to_iso_day(s: str) -> str:
    """Pepper date -> YYYY-MM-DD (handles ISO and dd/mm/yyyy)."""
    s = (s or "").strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    if "/" in s:
        d, m, y = s[:10].split("/")
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return s[:10]


async def fetch_pepper():
    creds = {
        "phoneNumber": os.environ["PEPPER_PHONE"],
        "password": os.environ["PEPPER_PASSWORD"],
        "otpLongTermToken": os.environ["PEPPER_TOKEN"].strip(),
    }
    s = create_scraper(provider="pepper", credentials=creds)

    async def no_sms() -> str:
        raise RuntimeError(
            "Pepper asked for a new SMS code - the saved connection key expired. "
            "Run pepper_check.py on the PC again and update the PEPPER_TOKEN secret."
        )

    s.on_otp_request = no_sms
    txns = []
    try:
        await s.initialize()
        await s.login()
        user_data = await s._query_graphql("UserDataV2", P.USER_DATA_V2_QUERY)
        now = datetime.now(UTC)
        frm = (now - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
        to = now.strftime("%Y-%m-%d")
        for acct in find_accounts(user_data, []):
            acc_id = str(acct.get("accountId") or "")
            acc_num = str(acct.get("accountNumber") or acc_id)
            last4 = acc_num[-4:]
            page = 1
            while True:
                data = await s._query_graphql(
                    "Transactions", P.TRANSACTIONS_QUERY,
                    {"accountId": acc_id, "from": frm, "to": to,
                     "pageNumber": page, "pageCount": P.PAGE_SIZE},
                )
                obj = first(first(data.get("accounts")).get("oshTransactionsNew"))
                done = obj.get("transactions") or []
                for t in done:
                    txns.append((last4, t))
                if len(done) < P.PAGE_SIZE:
                    break
                page += 1
    finally:
        try:
            await s.terminate()
        except Exception:
            pass
    return txns


def to_row(last4, t, header, today):
    tid = str(t.get("transactionId") or t.get("referenceNumber") or "")
    day = to_iso_day(str(t.get("bookingDate") or ""))
    eff = to_iso_day(str(t.get("effectiveDate") or t.get("bookingDate") or ""))
    amount = float(t.get("transactionAmount") or 0)
    desc = str(t.get("title") or t.get("description") or "").strip()
    memo = str(t.get("subTitle") or "").strip()
    y, m, d = day.split("-")
    raw = {
        "identifier": tid, "type": "normal", "status": "completed",
        "date": f"{day}T00:00:00.000Z", "processedDate": f"{eff}T00:00:00.000Z",
        "originalAmount": amount, "originalCurrency": "ILS",
        "chargedAmount": amount, "chargedCurrency": "ILS",
        "description": desc, "memo": memo,
        "companyId": "pepper", "accountNumber": last4,
    }
    values = {
        "date": f"{d}/{m}/{y}", "amount": amount, "description": desc, "memo": memo,
        "category": "", "account": last4, "hash": f"pepper_{last4}_{tid}",
        "comment": "", "scraped at": today, "scraped by": "pepper_sync",
        "identifier": tid, "chargedCurrency": "ILS",
        "raw": json.dumps(raw, ensure_ascii=False),
    }
    return [values.get(h, "") for h in header]


def main():
    cfg = json.loads(os.environ["MONEYMAN_CONFIG"])
    gs = cfg["storage"]["googleSheets"]
    sa = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": gs["serviceAccountEmail"],
            "private_key": gs["serviceAccountPrivateKey"].replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheet = gspread.authorize(sa).open_by_key(gs["sheetId"]).worksheet(WORKSHEET)

    txns = asyncio.run(fetch_pepper())
    print(f"Pepper: read {len(txns)} completed transactions")

    header = sheet.row_values(1)
    hash_col = header.index("hash") + 1
    existing = set(sheet.col_values(hash_col))
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    new_rows = []
    for last4, t in txns:
        row = to_row(last4, t, header, today)
        h = row[hash_col - 1]
        if h not in existing and not h.endswith("_"):
            existing.add(h)
            new_rows.append(row)
    if new_rows:
        sheet.append_rows(new_rows, value_input_option="RAW")
    print(f"Pepper: added {len(new_rows)} new rows, {len(txns) - len(new_rows)} already in the sheet")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Short message only - no data
        print(f"Pepper sync failed: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
