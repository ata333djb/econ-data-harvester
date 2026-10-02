# EconDataHarvester

**Pull Chinese macroeconomic data from five official sources — and prove you didn't get it wrong.**

[中文](README.md) · MIT · Python 3.11+

---

## What this is

A command-line tool plus a data-credibility layer. Give it an indicator name (`CPI`) and it
fetches the data from **NBS (National Bureau of Statistics) / World Bank / IMF / FRED / BIS**,
merges it into one clean table, and then answers the question most fetch scripts never ask:

> Do these sources actually agree? If not, where do they differ, why, and which one should I trust?

It does not interpolate, does not guess, and does not silently drop data.
**Every row traces back to the original HTTP response bytes.**

```bash
python tools/edh.py list                              # 18 indicators / 51 source mappings
python tools/edh.py fetch CPI --from 2020 --to 2024   # fetch from 4 sources + cross-check
python tools/edh.py export CPI --from 2020 --to 2024  # land it in the validated layer
```

---

## Why this exists

Two problems with Chinese macro data that rarely get mentioned:

**1. The same indicator means different things at different sources.**
The World Bank's China CPI is an index rebased to 2010=100. The NBS one is year-on-year
(previous year = 100). Both are called "CPI", **but plotting them on one axis is simply wrong.**
This project treats "are these units actually comparable?" as a question to be decided rather
than assumed — of the six source pairs available for `CPI`, only World Bank vs BIS is directly
comparable.

**2. Many "independent sources" are not independent.**
A full round of work went into checking whether China's CPI has any genuinely independent source
(`docs/ENGINEERING_NOTES.md` §3.10). The answer is **no**: BIS only splices and rebases, OECD
reprints NBS, and PWT/Maddison use OECD. The hardest single piece of evidence is that OECD's
China CPI growth is **identical digit-for-digit, year by year,** with the IMF WEO's `PCPIPCH` —
the signature of one NBS series reprinted twice.

**Anyone who doesn't know this thinks they're doing four-source cross-validation when they're
actually comparing one dataset against itself.**

---

## Quick start

You don't need Python installed if you use the [prebuilt zip](#no-python-the-standalone-zip).

```bash
git clone <this-repo> && cd econ-data-harvester

python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

export PYTHONPATH=$PWD/python      # Windows PowerShell: $env:PYTHONPATH="$PWD\python"
export PYTHONIOENCODING=utf-8      # required on Windows or you get mojibake

python tools/edh.py list
python tools/edh.py fetch CPI --from 2020 --to 2024 --output cpi.csv
```

`fetch` writes **CSV to stdout and the human-readable summary to stderr**, so redirection always
gives you clean data:

```bash
python tools/edh.py fetch CPI --from 2020 --to 2024 > cpi.csv   # CSV only
```

---

## What it produces

| Artifact | Location | What it is |
|---|---|---|
| Long-table JSON | `data/validated/<source>/` | Fixed 14-column schema, every row carries a `row_sha16` fingerprint |
| Missing-data metadata | `data/processed/` | Every gap classified, with a suggested action — **the value stays null** |
| CSV / SQLite / data dictionary | `data/output/` | For downstream use |
| Single-file HTML report | `data/output/report.html` | All charts and CSS inlined; opens offline, safe to email |
| Cross-check verdicts | `data/validated/cross_check/` | Period-by-period differences for each source pair |
| Comparability rulings | `data/validated/arbiter/` | Profile × measurement → four-value verdict |
| Credibility scores | `data/validated/credibility/` | Five-dimension weighted score, 0–100 |

---

## How data flows

```
5 sources ──> raw responses archived by content fingerprint ──> parse ──> 14-column long table
              data/raw/_http_cache/                                       data/validated/
                                                                               │
              missing-data classification ──> fill strategy (label only, never interpolate)
              data/validated/missing_report.json ──> data/processed/
                                                                               │
                                    CSV / SQLite / data dictionary / HTML report
                                                    data/output/
   Side channel: knowledge base (source_profiles.yaml) + lineage ──> profile ──> ruling ──> score
```

**Key design point: `data/raw/` is the root of the evidence chain.** Each HTTP response is stored
as `<sha16>.bin` plus a matching `.meta.json`, fingerprinted by `url+method+body`. Any conclusion
can be traced back to the original bytes, and a re-run can prove that *the data changed* rather
than *the code changed*.

---

## Deliberate trade-offs

The parts worth stealing:

**Never interpolate.** Missing is `null`. Gaps are classified into four kinds (`series_start` /
`discontinued` / `not_yet_published` / `true_gap`), each with a suggested action
(`leave_null` / `wait` / `interpolate`) — but **no code path currently returns `interpolate`**,
and one appearing raises `NotImplementedError`. Better to show the user a hole than a complete-looking
table that is quietly fabricated.

**"Row count" is not "has data".** For periods it hasn't published, the NBS returns **placeholder
rows**: the date is present, the value is an empty string. So all probing and validation counts
**non-empty values**. The first version judged by row count, passed three mappings, and one of them
was a false pass.

**Cross-validation does not silently normalise units.** Differing units produce a "different basis"
verdict and the absolute difference is labelled "informational only" — it **never** reports a hard
"conflict". String equality isn't enough either: `index (2010=100)` and `index (2010=100, monthly)`
are the same basis, while `index (previous year=100)` must stay distinct.

**HTTP uses the standard library only.** No `requests`, no `httpx` — the whole network layer is
`urllib` + `ssl`, and the only dependencies are PyYAML and Jinja2. This isn't purity for its own
sake: the development machine had a broken Windows Schannel credential store, and only an
interpreter carrying its own OpenSSL could reach the data sources. The project simply froze that
constraint in place.

**The gate really hits the network.** `tools/run-all-checks.py` runs 30 checks, several of which
**actually call the five sources** and assert on the responses. That makes it slow (~3 minutes) and
sensitive to upstream maintenance windows — so CI runs an offline subset and the full gate is run
locally before release. **This is intentional**: an all-mocked gate cannot prove you can still
fetch data today.

---

## Development

```bash
python tools/run-all-checks.py        # 30 checks, ~3 minutes, fail-fast
```

Run it after changing anything. The gate is **repeatable** — two runs on the same day must both be
30/30. If they aren't, you have a "downstream dependency" (a check reading an artifact produced by
a later check). That trap was hit once; it's written up in `docs/ENGINEERING_NOTES.md` §3.20.

Layout:

```
python/econ_core/     Runtime package (24 files, ~8.9k lines): five clients, normalisation,
                      missing data, profiling, ruling, scoring, splicing, catalog, fetch
                      adaptation, export
python/_probes/       Forensic scripts (19): how the API parameters were actually reverse-engineered
tools/                Processing and gate (17 files, ~4.8k lines)
src/plugins/          Five thin adapter shims: argv translation + stdout envelope translation
docs/ENGINEERING_NOTES.md   Engineering log: why, what was tried, what was falsified
```

**`docs/ENGINEERING_NOTES.md` is the most interesting part of this project.** It records falsified
assumptions ("guessing semantics from key names will eventually mismatch"), silent bugs ("the
deduplication in the materialisation scan never actually worked — half the statistics were double
counts"), and conclusions explicitly marked "do not reopen". **If you only read one file, read that one.**

---

## No Python? The standalone zip

`tools/make-dist-zip.py` builds a **bundled-Python, install-free package**:

```
edh.bat list
edh.bat info CPI
edh.bat fetch CPI --from 2020 --to 2024 --output cpi.csv
edh.bat pip install <package>        # if you need extra packages
```

Unzip and run — no Python install, no environment variables. The goal is to let non-programmers get
at the data (`dist/README.md` is the version written without any code, architecture or gate talk).
See `PACKAGING.md` for how to rebuild it.

---

## Optional: agent-harness integration

`.dsh/` and `tools/verify-preset.mjs` wire this project into the AI agent harness the original author
used. **They are not part of the public repository** — they hardcode absolute paths from that machine
and mean nothing to anyone else. If you see references to those paths elsewhere, ignore them; no data
functionality depends on them.

---

## Known limitations (read this before adopting)

- **China only.** All five source integrations are hardcoded for China (`CHN` / `全国`). Other
  countries need `catalog_data.yaml` edits and, for some clients, code changes.
- **18 indicators is not "all of economics".** The catalog is hand-curated: a mapping only counts
  once it has actually been fetched successfully, which makes expansion slow. That is deliberate —
  see §2.13.
- **Depends on upstream API stability.** All five are public endpoints and may change parameters or
  rate-limit without notice. The project is sensitive to this (hence the `_probes/` forensic record)
  but offers no SLA.
- **Cross-validation is a heuristic, not a judge.** It can tell you "these two sources differ by
  3 percentage points in 2012". It cannot tell you which is right. Rulings combine a hand-curated
  profile with measured differences, and `unknown` is a permitted outcome.
- **Not a data-vendor replacement.** No versioned datasets, no revision history tracking, no support.
- **`arbiter` thresholds are hand-set** (e.g. `RELATIVE_METRIC_FLOOR = 0.5` is calibrated for
  percentage-point magnitudes) and need re-calibration for other units.

---

## Data licensing

**The code is MIT; the data is not.** Data fetched at runtime belongs to its publishers
(NBS / World Bank CC BY 4.0 / IMF / FRED / BIS). Check the terms before redistributing — see `LICENSE`.

## License

MIT © 2026 ignshion
