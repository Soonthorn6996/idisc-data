"""Randomly sample listed Thai stocks and stress both datasets in production.

Goal is not "does it return 200" but "does it return data we understand":
unmapped Thai vocabulary shows up as method_code/relationship_code/
security_type_code falling back to 'other' or 'unknown', which is exactly the
kind of silent gap a fixed set of fixtures cannot catch.
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter

import httpx

BASE = "https://idisc-data-production.up.railway.app"
SAMPLE_SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 14
# Wide window so a sampled company yields as much vocabulary variety as possible.
DATE_FROM, DATE_TO = "20150101", "20261231"

client = httpx.Client(timeout=180.0)

print("Fetching symbol directory ...", flush=True)
directory = client.get(f"{BASE}/api/v1/symbols").json()["symbols"]
eligible = [s for s in directory if s["unique_id_reference"]]
print(f"  {len(directory)} companies, {len(eligible)} with a Form 59 id", flush=True)

sample = random.sample(eligible, SAMPLE_SIZE)
print(f"\nRandom sample of {SAMPLE_SIZE}: {', '.join(s['symbol'] for s in sample)}\n", flush=True)

methods: Counter = Counter()
relationships: Counter = Counter()
securities: Counter = Counter()
holder_types: Counter = Counter()
cac_statuses: Counter = Counter()
cg_scores: Counter = Counter()
agm_scores: Counter = Counter()

anomalies: list[str] = []
rows = []
total_records = 0

for index, entry in enumerate(sample, 1):
    symbol = entry["symbol"]
    row = {"symbol": symbol, "market": entry.get("market"),
           "sector": entry.get("sector_code")}

    # ---- Form 59 -------------------------------------------------------
    try:
        resp = client.get(
            f"{BASE}/api/v1/form59/{symbol}",
            params={"date_from": DATE_FROM, "date_to": DATE_TO,
                    "include_records": "true"},
        )
        if resp.status_code != 200:
            row["f59"] = f"HTTP {resp.status_code}"
            anomalies.append(f"{symbol}: form59 HTTP {resp.status_code}")
        else:
            payload = resp.json()
            meta = payload["meta"]
            records = payload["records"]
            total_records += len(records)
            row["f59"] = f"{meta['records_parsed']}/{meta['records_reported_by_source']}"
            row["f59_ok"] = meta["parse_complete"]

            if not meta["parse_complete"]:
                anomalies.append(
                    f"{symbol}: parse INCOMPLETE - page said "
                    f"{meta['records_reported_by_source']}, parsed {meta['records_parsed']}"
                )

            for record in records:
                methods[record["method_code"]] += 1
                relationships[record["relationship_code"]] += 1
                securities[record["security_type_code"]] += 1
                holder_types[record["holder_type"]] += 1

                # Fallback buckets mean vocabulary we have not mapped.
                if record["method_code"] in {"other", "unknown"}:
                    anomalies.append(
                        f"{symbol}: UNMAPPED method {record['method_th']!r}")
                if record["relationship_code"] in {"other", "unknown"}:
                    anomalies.append(
                        f"{symbol}: UNMAPPED relationship {record['relationship_th']!r}")
                if record["security_type_code"] in {"other", "unknown"}:
                    anomalies.append(
                        f"{symbol}: UNMAPPED security type {record['security_type_th']!r}")
                # Data-integrity invariants.
                if record["shares"] is not None and record["shares"] < 0:
                    anomalies.append(f"{symbol}: negative shares {record['shares']}")
                if record["transaction_date"] and not record["transaction_date"].startswith("20"):
                    anomalies.append(
                        f"{symbol}: implausible date {record['transaction_date']}")
                if (record["price_per_share"] is not None
                        and record["transaction_value"] is None):
                    anomalies.append(f"{symbol}: price present but value missing")
    except Exception as exc:  # noqa: BLE001
        row["f59"] = f"ERR {type(exc).__name__}"
        anomalies.append(f"{symbol}: form59 exception {exc}")

    # ---- Sustainability ------------------------------------------------
    try:
        resp = client.get(f"{BASE}/api/v1/sustainability/{symbol}")
        if resp.status_code != 200:
            row["sus"] = f"HTTP {resp.status_code}"
            anomalies.append(f"{symbol}: sustainability HTTP {resp.status_code}")
        else:
            payload = resp.json()
            block = payload["sustainability"]
            card = payload["sustainability_scorecard"]
            cg, agm = block["cg_score"], block["agm_level"]
            cg_scores[cg["score"]] += 1
            agm_scores[agm["score"]] += 1
            cac_statuses[block["thai_cac"]["status"]] += 1
            row["sus"] = (f"CG={cg['score'] or '-'} AGM={agm['score'] or '-'} "
                          f"ESG={block['set_esg_rating']['rating'] or '-'} "
                          f"({card['disclosed_indicators']}/4)")

            # A rated indicator must carry a label; an unrated one must not
            # masquerade as a score.
            if cg["is_rated"] and not cg["label_th"]:
                anomalies.append(f"{symbol}: CG rated but no label")
            if not cg["is_rated"] and cg["score"] is not None:
                anomalies.append(f"{symbol}: CG not rated yet has a score")
            if cg["score"] is not None and not 1 <= cg["score"] <= 5:
                anomalies.append(f"{symbol}: CG score out of range {cg['score']}")
            if agm["score"] is not None and not 1 <= agm["score"] <= 5:
                anomalies.append(f"{symbol}: AGM score out of range {agm['score']}")
            if not payload["company"]["company_name_th"]:
                anomalies.append(f"{symbol}: missing company name")
    except Exception as exc:  # noqa: BLE001
        row["sus"] = f"ERR {type(exc).__name__}"
        anomalies.append(f"{symbol}: sustainability exception {exc}")

    rows.append(row)
    print(f"  [{index:2d}/{SAMPLE_SIZE}] {symbol:<8} {row.get('market','?'):<4} "
          f"f59={row.get('f59','?'):<10} {row.get('sus','?')}", flush=True)

result = {
    "sample": [r["symbol"] for r in rows],
    "rows": rows,
    "total_form59_records": total_records,
    "methods": dict(methods),
    "relationships": dict(relationships),
    "securities": dict(securities),
    "holder_types": dict(holder_types),
    "cac_statuses": dict(cac_statuses),
    "cg_distribution": {str(k): v for k, v in sorted(cg_scores.items(), key=lambda x: (x[0] is None, x[0]))},
    "agm_distribution": {str(k): v for k, v in sorted(agm_scores.items(), key=lambda x: (x[0] is None, x[0]))},
    "anomalies": anomalies,
}
with open("random_result.json", "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False, indent=2)

print(f"\nTotal Form 59 records inspected: {total_records}")
print(f"Anomalies: {len(anomalies)}")
client.close()
