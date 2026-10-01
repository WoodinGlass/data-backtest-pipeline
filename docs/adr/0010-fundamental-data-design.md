# 10. Fundamental data design: SEC EDGAR XBRL (filing-date PIT)

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The feature engineering layer (M4) needs company fundamentals:
revenue, earnings, margins, capex, inventory, headcount, and roughly
150 other items that describe the operating state of a business.

Unlike macro data, fundamentals have two PIT subtleties:

1. **Filing delay.** A company's Q4 2023 10-K is filed roughly 45 days
   after quarter end. The information did not exist on 2024-01-01.
2. **Restatements.** A company may file a 10-K/A that restates a prior
   period. The original filing is what traders actually saw at the
   time and must remain in the raw layer.

The source must be authoritative, free, and stable. SEC EDGAR is the
regulator's own archive.

## Decision

### Source

**SEC EDGAR** via the `companyfacts` API
(`https://data.sec.gov/api/xbrl/companyfacts/CIK<10-digit>.json`).
One response contains the full XBRL fact history for one company.

Alternate sources considered and rejected: FMP, Alpha Vantage, YFinance.
They are downstream of EDGAR, rate-limited on free tiers, and do not
preserve vintage. For a portfolio project emphasizing reproducibility,
the primary source wins.

### Curation

~150 XBRL tags, curated in `config/fundamental_tags.yml`:

| Category             | Count | Example tags                                          |
|----------------------|-------|-------------------------------------------------------|
| Income statement     | ~40   | Revenues, CostOfRevenue, GrossProfit, NetIncomeLoss,  |
|                      |       | ResearchAndDevelopmentExpense, EarningsPerShareBasic  |
| Balance sheet        | ~40   | Assets, Liabilities, StockholdersEquity, InventoryNet,|
|                      |       | PropertyPlantAndEquipmentNet, LongTermDebt, Goodwill  |
| Cash flow            | ~25   | NetCashProvidedByUsedInOperatingActivities,           |
|                      |       | PaymentsToAcquirePropertyPlantAndEquipment            |
| Ratio inputs         | ~25   | CurrentAssets, CurrentLiabilities, AccountsReceivable |
| Per-share            | ~10   | WeightedAverageNumberOfSharesOutstandingBasic         |
| Employees            | 1     | Extracted from 10-K cover page (regex)                |
| Macro-correlated     | ~10   | InterestExpense, IncomeTaxExpenseBenefit,             |
|                      |       | EffectiveIncomeTaxRate, ForeignCurrencyTransaction... |

### Employees extraction

SEC requires public companies to disclose headcount on the 10-K cover
page. Wording varies:

- "As of December 31, 2023, the Company had approximately 161,000
  employees."
- "The Company employed 45,300 full-time employees as of December 31,
  2023."

We use a curated regex with several variants plus a numeric sanity
check (1 < n < 5,000,000). Extraction is best-effort: failures are
logged and the value is NULL for that filing. A quality-gate check
reports the coverage rate and warns if coverage falls below 60%.

### Storage

Immutable raw Parquet layer, per (ticker, filing):

```
data/raw/fundamentals/
├── manifest.json
└── sec/
    ├── AAPL/
    │   ├── <hash>.parquet
    │   └── ...
    └── ...
```

Each snapshot is one `companyfacts` response at one point in time.
An amendment produces a new snapshot; the manifest records filing
accessions so a fact can be traced back to its source filing.

### PIT join

`int_fundamentals_pit` performs an as-of join to the trading calendar:

```
For each (ticker, trade_date):
    pick facts from the LATEST filing where
        filing_date <= trade_date
        and period_end <= filing_date
        and form_type in ('10-K', '10-Q')
```

Facts from a future filing are never visible. A Pandera check on
`fct_fundamentals_daily` asserts that the maximum `filing_date` used
to populate any row is `<= trade_date`.

### Warehouse layers

- `staging.stg_sec_facts` — long format `(ticker, tag, period_end,
  filing_date, form_type, value, accession_number)`.
- `intermediate.int_fundamentals_pit` — as-of join to the trading
  calendar.
- `marts.fct_fundamentals_daily` — wide format, one column per tag,
  forward-filled from the most recent filing as-of each trade date.

## Consequences

**Positive**

- PIT correctness with respect to filings. No look-ahead.
- Rich feature set spanning operating, financial, and capital structure.
- Primary source: the SEC's own archive.

**Negative / trade-offs**

- Coverage varies by sector: some tags (InventoryNet) apply to retailers
  but not banks; others (UnearnedRevenue) to SaaS but not industrials.
  Downstream consumers must tolerate NULLs.
- Employee extraction is text-based. Target coverage 60–80%, not 100%.
- EDGAR API is rate-limited (10 req/s). 32 tickers ingest in seconds;
  a 500-ticker universe would need batching.
- Restatements mean `fct_fundamentals_daily` cannot be reconstructed
  from any single "current" source. Raw snapshots preserve history.

**Explicitly rejected**

- *FMP / Alpha Vantage.* Free tiers are restrictive; both are
  downstream of EDGAR. We prefer the primary source.
- *Quarterly-only granularity.* Rejected: some fundamentals update
  more often (shares outstanding), and filing-date PIT is needed
  regardless.
- *Skip employee extraction.* Rejected: headcount is explicitly
  required as a feature; extraction scope is bounded.

## Related

- ADR 0006 — content hash excludes vendor noise (applies here as well).
- ADR 0009 — macro data design (FRED + ALFRED).
- M1 — ingestion pattern.
