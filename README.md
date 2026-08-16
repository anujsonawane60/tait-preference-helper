# TAIT School Preference Helper

Helps a TAIT candidate decide the order to fill school preferences in. Upload the
`GeneratedPreferences_*.pdf` from the portal, pick your subject and category, and get a
ranked table of every school on your list — with its vacancy count in your subject and
its reservation position in your category — downloadable as CSV.

## How to run

Open PowerShell in this folder (`C:\Users\Hp\Desktop\My_Project\sk`) and run:

```powershell
.\venv\Scripts\Activate.ps1     # the bundled venv already has every dependency
python -m streamlit run app.py
```

That opens <http://localhost:8501> in your browser. Press `Ctrl+C` in the terminal to stop
it. If PowerShell blocks the activate script, either run
`Set-ExecutionPolicy -Scope Process RemoteSigned` first, or skip the venv entirely — the
dependencies are installed system-wide too, so `python -m streamlit run app.py` works on
its own.

Setting up from scratch on another machine:

```powershell
pip install -r requirements.txt
python tait_engine.py build all           # both corpora, ~2 min, then never again
python -m streamlit run app.py
```

### Using the app

1. **Enter your name and category** once — they apply to both streams. Tick the
   **divyang (PwD) quota** box if it applies to you and pick your disability type.
2. **Upload** your `GeneratedPreferences_*.pdf`. There are two boxes, **With Interview**
   and **Without Interview**; fill in whichever you have, or both.
3. **Adjust subject / post level** if needed — they arrive pre-ticked from your own PDF.
4. **Download** as **CSV or PDF** — each stream offers both, e.g.
   `Paul_Kulkarni_TAIT_OBC_WithInterview.csv` / `.pdf`, so several candidates can keep
   their lists in one folder without overwriting each other. Marathi names work; leave the
   name blank and the file is just `TAIT_<CATEGORY>_<Stream>`.

   - **CSV** carries every column, for sorting and filtering in Excel.
   - **PDF** is a printable landscape sheet with repeating column headers and page
     numbers — meant to be worked down while filling the portal. A 631-row list comes to
     about 22 pages.

   The PDF prints Latin text only. School names, districts and subjects are already Latin
   in the source PDFs, so only the candidate's own name can be non-Latin: ReportLab does
   no complex-script shaping, so a Devanagari name is drawn with an embedded Marathi font
   and flagged with a caution that matras may sit wrongly. The CSV is unaffected.

Both PDF folders are only ever read, never written to, so the same copy serves every
candidate.

## The two streams

| | With Interview | Without Interview |
|---|---|---|
| Folder | `ALL_PDF's` | `ALL_PDF's_Without_Interview` |
| Cache | `cache/schools_with_interview.json` | `cache/schools_without_interview.json` |
| Advertisements | 2,416 | 309 |
| Employers | private aided (`PA`) | `ZP` zilla parishad, `MP` / `MC` municipal, `TD`, `PA` |

They are cached separately rather than merged because **a school code can appear in both**
corpora, so one combined index would collide. The `Employer` column is shown in the
results, and the without-interview stream also gets an employer-type filter.

School codes differ between the streams. Private schools use `270103SC008`
(6 digits + 2 letters + 3 digits); government employers use forms like `2701DYCTD`,
`2702NPP02`, `2719DDAO`. Nothing in the parser assumes the private shape — the code is
simply everything before the medium in the filename, and in the preference PDF it is the
bracketed token after the school name.

Useful extras:

```powershell
python tait_engine.py info               # what's in the cache right now
```

Each corpus is parsed **once** into its own cache (3.3 MB and 0.4 MB). Every analysis after that
is a lookup, not a PDF parse, so it returns in under a second no matter how many people
use it. Rebuild only when the advertisement PDFs change — sidebar button, or re-run the
`build` command.

If you ever move or rename the PDF folder, either pass the new path to `build` or type it
into the **Corpus folder** box in the sidebar and hit *Rebuild corpus cache*.

## What the source PDFs actually contain

Each school advertisement holds two tables that **do not cross-reference each other**:

| | Page 1 — reservation roster | Page 2 — vacancy table |
|---|---|---|
| Granularity | the **whole school**, all levels and subjects combined | per (designation, subject) |
| Content | 13 category rows × 8 columns | posts per subject |

So the PDF tells you *"this school has 6 Maths-Science posts"* and *"this school has 10 OPEN
posts"* — but **never** which category any particular Maths-Science post belongs to. That
cross-tab does not exist in the source.

The tool therefore reports `Subject Posts` and `Category Posts` as separate columns. Read
them side by side; do not multiply or intersect them.

## Output columns

| Column | Meaning |
|---|---|
| `School Code`, `School Name`, `District` | from your preference PDF |
| `Level`, `Medium` | the advertisement matched (see *Matching* below) |
| `Subject`, `Designation`, `Aid Type` | the vacancy row matched |
| `Subject Posts` | vacancies in that subject at that level |
| `Category` | the category you selected |
| `Category Posts` | that category's total in the school-wide roster |
| `Category Female`, `Category Sports`, … | parallel quotas *inside* `Category Posts` — a subset, not an addition |
| `OPEN Posts` | the OPEN row's total, shown on every sheet (omitted only when OPEN *is* your category) |
| `OPEN Female`, … | the same parallel quotas inside `OPEN Posts` |
| `Divyang OH / HI / VI / ID / MD` | divyang posts by type, your own type first. Only when the quota box is ticked |
| `Divyang Total`, `Orphan Total` | the totals those blocks print |
| `School Total` | all advertised posts at the school |
| `Match` | `Yes` if the school has a vacancy in your selected subject(s) |
| `Eligible As` | the subject(s) your own preference PDF lists for this school |
| `Status` | blank when clean; otherwise the parsing or matching caveat |

Every school from your preference PDF appears in the output. Schools with no vacancy in
your subject come back with `Match = No` and `0` posts rather than being dropped, so you can
still see and re-sort the full list.

**OPEN is always shown.** A reserved-category candidate competes in OPEN as well, and the
two blocks vary independently — `272016SC001` advertises 68 OBC posts and **zero** OPEN out
of 190, while other schools are the reverse. The category column alone cannot tell those
apart, so both are printed side by side. Each parallel quota you tick is likewise shown for
both blocks: a female OBC candidate gets `Category Posts`, `Category Female`, `OPEN Posts`
and `OPEN Female`.

The printed sheet keeps the same figures but heads them by block — `OBC`, `OBC-F`, `OPEN`,
`OPEN-F` — because a bare `Female` next to two totals is exactly the column somebody reads
against the wrong one.

## The divyang (दिव्यांग) quota

Below the 13 roster rows each advertisement prints a **दिव्यांग (4%)** block and an
**अनाथ (1%)** block, both outside the 8-column grid. They carry no serial number, so they
are found by the `(1%)` / `(4%)` markers and the `ID` / `MD` tokens — all Latin, so none of
this depends on the broken Devanagari cmap. Five buckets, read left to right by position:

| Code | Type | Marathi |
|---|---|---|
| `OH` | Orthopaedic | अस्थिव्यंग |
| `HI` | Hearing Impaired | कर्णबधिर |
| `VI` | Blind / Low Vision | अंध/अल्पदृष्टी |
| `ID` | Intellectual Disability | — |
| `MD` | Multiple Disability | — |

The block prints its own total, so the build self-checks the five buckets against it the
same way the vacancy table is checked. **All 2,725 files parse and reconcile; zero errors.**

Two things the tool deliberately will not do:

- **It never derives a seat count from the 4% share.** The roster prints the real figures,
  and 4% of the school total disagrees with them on most schools once rounding and
  carry-forward are applied. Only the printed numbers are reported.
- **The certificate percentage gates eligibility and nothing else.** The candidate's own
  percentage (40% is the benchmark threshold) appears in no advertisement PDF anywhere, so
  it cannot change any school's post count. Below 40% the app says the quota does not apply
  and falls back to the normal category view.

Divyang is a **parallel** reservation — carved out of the category totals beside it, not
added to them, exactly like the female and sports quotas.

**These posts are scarce.** Only 174 of 2,416 with-interview schools (7%) advertise one, for
217 posts total, and 173 of those are `OH`. The without-interview stream is denser —
110 of 309 schools (36%), 758 posts, spread far more evenly across the five types. Ticking
*show only schools with a post in my type* can cut a 600-school list to a handful, so it is
off by default.

## Matching

A school code alone is **not** unique — 125 codes in the corpus have both a primary (`_P_`)
and a secondary (`_S_`) advertisement, each with its own reservation roster. Matching
narrows in this order:

1. code + level + medium
2. code + level
3. code alone

Anything looser than an exact match is recorded in the `Status` column.

## Category rows

Categories are read by **row serial number**, not by their Marathi label. The PDFs embed a
broken font cmap (`इ.मा.व` extracts as `इ.म֞ .व`), so text matching on Devanagari is not
reliable; the row order is.

| Row | Category | | Row | Category |
|---|---|---|---|---|
| 1 | SC (अनु.जाती) | | 8 | OBC (इ.मा.व) |
| 2 | ST (अनु.जमाती) | | 9 | SBC (वि.मा.प्र) |
| 3 | ST-PESA | | 10 | EWS (ई.डब्लू.एस) |
| 4 | VJ-A / DT-A (वि.जा.(अ)) | | 11 | SEBC (एसईबीसी) |
| 5 | NT-B (भ.ज.(ब)) | | 12 | OPEN (खुला) |
| 6 | NT-C (भ.ज.(क)) | | 13 | TOTAL (एकूण) |
| 7 | NT-D (भ.ज.(ड)) | | | |

## Files

| File | Purpose |
|---|---|
| `tait_engine.py` | all PDF parsing, caching, matching and analysis; no UI dependencies |
| `tait_report.py` | printable PDF report layout; no UI dependencies |
| `app.py` | Streamlit front end |
| `requirements.txt` | PyMuPDF, streamlit, pandas, reportlab |
| `ALL_PDF's/` | 2,416 with-interview advertisements — **read-only**, never written to |
| `ALL_PDF's_Without_Interview/` | 309 without-interview advertisements — **read-only** |
| `cache/*.json` | parsed corpora (generated; delete and rebuild any time) |
| `ad_prompt.md` | ChatGPT prompt + draft for advertising the service |
| `GeneratedPreferences_*.pdf` | one real preference PDF, kept as a test file |
| `venv/` | virtual environment with the dependencies installed |

The earlier one-off scripts (`extract_pade.py`, `move_matching_pdfs.py`) and their
input/output CSVs have been removed. They are superseded: the engine reads the corpus in
place so no PDF copying step is needed, and it takes school codes and names straight from
the preference PDF instead of a hand-maintained `school_codes.csv`.

## Accuracy

The parser was cross-checked against the old `extract_pade.py` output on the 609 files both
had processed — **609/609 identical** on OBC totals and on Maths / Science / Maths-Science
counts. It also verifies itself on every build, twice over:

- each file's summed post count against the `Total` the vacancy table prints;
- the five divyang buckets against the total the divyang block prints.

Either disagreement is flagged as `PARSE ERROR`. Across all 2,725 files in both corpora
there are currently **zero**, and every file yields a divyang block.
