"""TAIT preference helper - PDF parsing engine.

Two kinds of PDF are involved:

  * the candidate's `GeneratedPreferences_*.pdf` - the list of schools/posts
    that candidate is eligible for (school name + code, district, level,
    medium, designation, subject).

  * one advertisement PDF per school, sitting in the corpus folder. Each holds
    two tables that DO NOT cross-reference each other:
      page 1  reservation roster - 13 category rows x 8 columns, covering the
              WHOLE school (every level and subject combined).
      page 2  vacancy table - posts per (designation, subject).
    There is no per-subject category breakdown anywhere in the source, so both
    numbers are reported side by side rather than merged into one.

The Devanagari in these PDFs uses a broken font cmap ('इ.मा.व' extracts as
'इ.म֞ .व'), so nothing is matched on Marathi text. Categories are identified by
their row serial number, which is stable across the corpus.

There are two separate recruitment streams, each with its own corpus folder and its
own cache: 'with' interview (private managements) and 'without' interview (mostly
zilla parishad, municipal and other government employers). They are kept apart
because a school code can appear in both.

CLI:
    python tait_engine.py build all       # parse both corpora
    python tait_engine.py build with      # just the with-interview corpus
    python tait_engine.py build without   # just the without-interview corpus
    python tait_engine.py info            # summary of every cache
"""

import json
import os
import re
import sys
import threading
import unicodedata
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings('ignore')

HERE = os.path.dirname(os.path.abspath(__file__))

# The two recruitment streams. Separate caches on purpose: at least one school
# code appears in both corpora, so a single merged index would collide.
CORPORA = {
    'with': {
        'label': 'With Interview',
        'folder': os.path.join(HERE, "ALL_PDF's"),
        'cache': os.path.join(HERE, 'cache', 'schools_with_interview.json'),
    },
    'without': {
        'label': 'Without Interview',
        'folder': os.path.join(HERE, "ALL_PDF's_Without_Interview"),
        'cache': os.path.join(HERE, 'cache', 'schools_without_interview.json'),
    },
}

# <code>_<medium>_<P|S>_<employer>   _With Interview.pdf
# The code is NOT always the private-school '6 digits + 2 letters + 3 digits'
# shape - government employers use forms like 2701DYCTD, 2702NPP02, 2719DDAO.
# So the code is simply 'everything before the medium'. Verified to produce
# results identical to the old strict pattern on all 2,416 with-interview files,
# while also matching all 309 without-interview ones. Padding spaces show up in
# odd places ('270301SC052  _Marathi_S_PA'), hence the \s* around separators.
FILENAME_RE = re.compile(
    r'^(.+?)\s*_\s*([A-Za-z]+)\s*_\s*([PS])\s*_\s*([A-Za-z0-9]+)', re.IGNORECASE)

# In the candidate's preference PDF the code is bracketed after the school name:
# 'INDIRA SHIKSHAN SANSTHA MANGRUL (272907SC032)'. Requiring 4 leading digits and
# at least one letter keeps it from matching the pay scale's '(18000)'.
PREF_CODE_RE = re.compile(r'\((\d{4}[0-9A-Za-z]{2,20})\)')
DESIGNATION_RE = re.compile(r'Teacher|Lecturer|Instructor|HeadMaster|Principal', re.IGNORECASE)

# PyMuPDF is NOT thread-safe, and a Streamlit server runs every visitor's script
# in its own thread. Parsing two preference PDFs at the same moment silently
# corrupts both - measured: a 1,098-row file came back as 487, 489 and 334 rows
# on three concurrent runs, with no exception raised. Every entry point that
# touches fitz holds this lock, so parses queue instead of interleaving.
# The corpus build is unaffected: it uses separate processes, not threads.
_FITZ_LOCK = threading.Lock()

# Reservation roster rows, keyed by the serial number in column 1.
CATEGORIES = {
    1:  ('SC',          'अनु.जाती'),
    2:  ('ST',          'अनु.जमाती'),
    3:  ('ST (PESA)',   'अनु.जमाती (PESA)'),
    4:  ('VJ-A / DT-A', 'वि.जा.(अ)'),
    5:  ('NT-B',        'भ.ज.(ब)'),
    6:  ('NT-C',        'भ.ज.(क)'),
    7:  ('NT-D',        'भ.ज.(ड)'),
    8:  ('OBC',         'इ.मा.व'),
    9:  ('SBC',         'वि.मा.प्र'),
    10: ('EWS',         'ई.डब्लू.एस'),
    11: ('SEBC',        'एसईबीसी'),
    12: ('OPEN',        'खुला'),
    13: ('TOTAL',       'एकूण'),
}
CATEGORY_ORDER = [CATEGORIES[n][0] for n in sorted(CATEGORIES)]
CATEGORY_BY_LABEL = {CATEGORIES[n][0]: n for n in CATEGORIES}

# The 8 numeric columns of the reservation roster, in order. The first six are
# parallel (horizontal) reservations carved out of the category's own quota -
# they are a subset of TOTAL, not an addition to it.
PARALLEL_COLUMNS = [
    'Female', 'Ex-Serviceman', 'Part-Time', 'Project-Affected',
    'Earthquake-Affected', 'Sports', 'Non-Parallel', 'TOTAL',
]
PARALLEL_INDEX = {name: i for i, name in enumerate(PARALLEL_COLUMNS)}


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def squash(cell):
    """Strip all whitespace, undoing the mid-word wrapping the PDFs produce.

    'Mathemati\ncs-Science' -> 'Mathematics-Science'
    """
    return re.sub(r'\s+', '', cell or '')


def compact(row):
    """Squash every cell and drop the empties.

    Table detection splits some pages into 22 columns instead of 10, padding
    with blanks. Dropping empties restores the canonical column order.
    """
    return [c for c in (squash(c) for c in row) if c]


def prettify(key):
    """Turn a squashed token back into something readable.

    'Language(English)'        -> 'Language (English)'
    'GraduateTeacherstd(9-10)' -> 'Graduate Teacher std (9-10)'
    """
    if not key:
        return key
    s = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', key)
    s = re.sub(r'(?<=[a-zA-Z])\(', ' (', s)
    s = re.sub(r'(?<=[a-z])std', ' std', s)
    return re.sub(r'\s+', ' ', s).strip()


def safe_filename(name, limit=40):
    """Turn a typed name into something safe to use as a filename.

    Keeps letters and digits, and combining marks alongside them - `\\w` alone
    would drop the matras out of a Devanagari name, turning 'पॉल कुलकर्णी' into
    'प_ल_क_लकर_ण'. Everything else collapses to a single underscore, which also
    disarms path separators and '..'. Returns '' when nothing usable is left, so
    the caller can fall back to a default instead of writing '_.csv'.
    """
    kept = [c if (c.isalnum() or unicodedata.category(c).startswith('M')) else '_'
            for c in (name or '')]
    return re.sub(r'_+', '_', ''.join(kept)).strip('_')[:limit].strip('_')


def level_code(text):
    """Map a preference-PDF Level value onto the P/S letter used in filenames."""
    t = squash(text).lower()
    if t.startswith('prim'):
        return 'P'
    if t.startswith('second'):        # the PDFs spell it 'Secondry'
        return 'S'
    return ''


# --------------------------------------------------------------------------
# school advertisement PDF
# --------------------------------------------------------------------------

def _reservation_row(cells):
    """Return (serial, [8 counts]) if this row is a roster row, else None."""
    serial = re.sub(r'\D', '', cells[0])
    if not serial or int(serial) not in CATEGORIES:
        return None
    nums = [int(c) for c in cells[1:] if c.isdigit()]
    if len(nums) < 8:
        return None
    return int(serial), nums[-8:]      # trailing 8 == the roster columns


def _vacancy_row(cells):
    """Return a raw vacancy dict if this row is a post row, else None.

    Canonical column order after compaction:
        [sr, medium, designation, pay scale, aid, subject, teaching medium,
         edu qual, prof qual, TET, posts]
    Subject is read from two independent anchors - three cells after the
    designation, and six from the end. When they agree the row is trustworthy
    and its subject seeds the corpus vocabulary; when they disagree the caller
    resolves it against that vocabulary.
    """
    idx = next((i for i, c in enumerate(cells) if DESIGNATION_RE.search(c)), None)
    if idx is None:
        return None
    posts = cells[-1]
    if not posts.isdigit():
        tail = [c for c in cells[idx + 1:] if c.isdigit()]
        if not tail:
            return None
        posts = tail[-1]

    from_left = cells[idx + 3] if idx + 3 < len(cells) else ''
    from_right = cells[-6] if len(cells) >= 6 else ''
    return {
        'designation': cells[idx],
        'medium': cells[1] if idx >= 2 else '',
        'aid': cells[idx + 2] if idx + 2 < len(cells) else '',
        'subject_left': from_left,
        'subject_right': from_right,
        'posts': int(posts),
    }


def parse_school_pdf(path):
    """Parse one school advertisement PDF into a record dict."""
    import fitz

    name = os.path.basename(path)
    m = FILENAME_RE.match(name)
    rec = {
        'code': m.group(1).strip().upper() if m else '',
        'medium': m.group(2).title() if m else '',
        'level': m.group(3).upper() if m else '',
        'employer': m.group(4).upper() if m else '',
        'file': name,
        'path': path,
        'reservation': {},
        'vacancies': [],
        'school_total': 0,
        'printed_total': None,
        'notes': [],
    }
    if not rec['code']:
        rec['notes'].append('no school code in filename')
        return rec

    try:
        doc = fitz.open(path)
    except Exception as e:
        rec['notes'].append(f'open failed: {e}')
        return rec

    with doc:
        for page in doc:
            text = page.get_text()
            # Page 1 carries the roster; the vacancy table is wherever a
            # designation appears. Everything else is boilerplate.
            if page.number != 0 and not DESIGNATION_RE.search(squash(text)):
                continue
            try:
                tables = page.find_tables().tables
            except Exception as e:
                rec['notes'].append(f'p{page.number + 1} table detection failed: {e}')
                continue

            for table in tables:
                for row in table.extract():
                    cells = compact(row)
                    if not cells:
                        continue

                    if page.number == 0:
                        hit = _reservation_row(cells)
                        if hit and hit[0] not in rec['reservation']:
                            rec['reservation'][hit[0]] = hit[1]
                            continue

                    # The vacancy table prints its own total; keeping it lets the
                    # parser be checked against the PDF's own arithmetic.
                    if cells[0].lower() == 'total' and cells[-1].isdigit():
                        rec['printed_total'] = int(cells[-1])
                        continue

                    vac = _vacancy_row(cells)
                    if vac:
                        rec['vacancies'].append(vac)

    if not rec['reservation']:
        rec['notes'].append('reservation roster not found')
    if not rec['vacancies']:
        rec['notes'].append('no vacancy rows found')
    rec['school_total'] = sum(v['posts'] for v in rec['vacancies'])

    # Parser self-check: our sum must equal the total the PDF prints itself.
    # A failure here is a parsing bug and is worth shouting about.
    if rec['printed_total'] is not None and rec['printed_total'] != rec['school_total']:
        rec['notes'].append(
            f'PARSE ERROR: summed {rec["school_total"]} posts but the PDF prints '
            f'{rec["printed_total"]}')

    # Not a parsing bug: on ~11% of files the school-wide roster counts more
    # posts than the vacancy table advertises. Both numbers are the PDF's own,
    # so the discrepancy is reported rather than reconciled.
    roster_total = rec['reservation'].get(13, [0] * 8)[PARALLEL_INDEX['TOTAL']]
    if rec['reservation'] and roster_total != rec['school_total']:
        rec['notes'].append(
            f'roster counts {roster_total} posts, vacancy table advertises '
            f'{rec["school_total"]}')
    return rec


# --------------------------------------------------------------------------
# corpus cache
# --------------------------------------------------------------------------

def find_pdfs(folder):
    """Every PDF under folder, recursively, sorted."""
    out = []
    for root, _dirs, files in os.walk(folder):
        out += [os.path.join(root, f) for f in files if f.lower().endswith('.pdf')]
    return sorted(out)


def _resolve_subjects(records):
    """Fill in each vacancy's final subject using a corpus-wide vocabulary.

    Rows whose two anchors agree define the vocabulary; the rest are matched
    against it. This keeps the parser open to subjects nobody has listed yet
    instead of hard-coding a whitelist that silently drops the unknown ones.
    """
    vocab = Counter()
    for rec in records:
        for v in rec['vacancies']:
            if v['subject_left'] and v['subject_left'] == v['subject_right']:
                vocab[v['subject_left']] += 1

    unresolved = 0
    for rec in records:
        for v in rec['vacancies']:
            left, right = v['subject_left'], v['subject_right']
            if left and left == right:
                subject = left
            elif left in vocab:
                subject = left
            elif right in vocab:
                subject = right
            else:
                subject = left or right
                if subject not in vocab:
                    unresolved += 1
                    rec['notes'].append(f'unrecognised subject {subject!r}')
            v['subject'] = subject
            v['subject_display'] = prettify(subject)
            v['designation_display'] = prettify(v['designation'])
            del v['subject_left'], v['subject_right']
    return vocab, unresolved


def build_cache(folder, cache_path, workers=None, progress=None):
    """Parse every PDF under `folder` once and write the cache.

    `progress` is an optional callable(done, total) for UI feedback.
    """
    files = find_pdfs(folder)
    if not files:
        raise SystemExit(f'No PDFs found under {folder}')
    workers = workers or max(1, (os.cpu_count() or 4) - 1)

    records = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, rec in enumerate(pool.map(parse_school_pdf, files, chunksize=8), 1):
            records.append(rec)
            if progress and (n % 25 == 0 or n == len(files)):
                progress(n, len(files))

    vocab, unresolved = _resolve_subjects(records)

    # The absolute path is only useful while parsing. Keeping it would write the
    # builder's own directory into the cache 2,700 times - noise in the file, and
    # a machine's local paths leaking anywhere the cache is shared.
    for rec in records:
        rec.pop('path', None)

    payload = {
        'source_folder': os.path.basename(os.path.abspath(folder)),
        'file_count': len(files),
        'subjects': sorted(vocab),
        'schools': records,
    }
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    payload['unresolved_subjects'] = unresolved
    return payload


def load_cache(cache_path):
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, encoding='utf-8') as f:
        payload = json.load(f)
    # JSON has no integer keys, so the roster comes back keyed by '1'..'13'.
    # Restore the ints the rest of the module indexes with.
    for rec in payload['schools']:
        rec['reservation'] = {int(k): v for k, v in rec['reservation'].items()}
    return payload


def index_cache(cache):
    """Build lookup tables: exact (code, level, medium), (code, level), and code."""
    idx = {'exact': {}, 'code_level': defaultdict(list), 'code': defaultdict(list)}
    for rec in cache['schools']:
        if not rec['code']:
            continue
        idx['exact'].setdefault((rec['code'], rec['level'], rec['medium']), rec)
        idx['code_level'][(rec['code'], rec['level'])].append(rec)
        idx['code'][rec['code']].append(rec)
    return idx


def lookup(idx, code, level, medium):
    """Find the advertisement for a school, narrowing from exact to loosest.

    A code alone is not unique: 125 codes in the corpus have both a primary and
    a secondary advertisement, each with its own roster.
    """
    rec = idx['exact'].get((code, level, medium))
    if rec:
        return rec, ''
    same_level = idx['code_level'].get((code, level)) or []
    if len(same_level) == 1:
        return same_level[0], f'medium fell back to {same_level[0]["medium"]}'
    if same_level:
        return same_level[0], f'{len(same_level)} files for this code+level; used {same_level[0]["file"]}'
    any_file = idx['code'].get(code) or []
    if len(any_file) == 1:
        return any_file[0], f'level fell back to {any_file[0]["level"]}'
    if any_file:
        return any_file[0], f'{len(any_file)} files for this code; used {any_file[0]["file"]}'
    return None, 'no advertisement PDF found for this school'


# --------------------------------------------------------------------------
# candidate preference PDF
# --------------------------------------------------------------------------

def parse_preference_pdf(source):
    """Read the candidate's GeneratedPreferences PDF.

    `source` is a path or a bytes-like object (Streamlit upload). Returns a
    list of dicts, one per listed school-post.

    Serialised on _FITZ_LOCK - see the note there. Two candidates uploading at
    the same instant would otherwise both receive a truncated list.
    """
    import fitz

    with _FITZ_LOCK:
        return _parse_preference_pdf_locked(fitz, source)


def _parse_preference_pdf_locked(fitz, source):
    if isinstance(source, (bytes, bytearray)):
        doc = fitz.open(stream=bytes(source), filetype='pdf')
    else:
        doc = fitz.open(source)

    rows = []
    with doc:
        for page in doc:
            try:
                tables = page.find_tables().tables
            except Exception:
                continue
            for table in tables:
                for row in table.extract():
                    cells = [re.sub(r'\s+', ' ', (c or '')).strip() for c in row]
                    keys = [squash(c) for c in cells]
                    if len(cells) < 10:
                        continue
                    # First cell holding a bracketed code that contains a letter.
                    # Matching on digits alone would drop every government school
                    # (2719DDAO, 2706EO01) while looking like it had worked.
                    hit = code = None
                    for i, key in enumerate(keys):
                        found = [c for c in PREF_CODE_RE.findall(key)
                                 if re.search(r'[A-Za-z]', c)]
                        if found:
                            hit, code = i, found[-1].upper()
                            break
                    if code is None:
                        continue
                    # Strip only the code's own bracket. Stripping '()' wholesale
                    # would eat the closing bracket of a real suffix - 'Education
                    # Officer Primary GONDIYA (B)' and '... (A)' are different
                    # offices, and '(B' reads like a truncation bug.
                    name = re.sub(r'\(' + re.escape(code) + r'\)', '', cells[hit],
                                  flags=re.IGNORECASE).strip()
                    rows.append({
                        'sr': cells[0],
                        'district': cells[1],
                        'school_name': re.sub(r'\s+', ' ', name),
                        'code': code,
                        'level': keys[3],
                        'level_code': level_code(keys[3]),
                        'medium': keys[4].title(),
                        'designation': prettify(keys[5]),
                        'subject': keys[8],
                        'subject_display': prettify(keys[8]),
                    })
    return rows


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def available_options(prefs, idx):
    """What the candidate can actually filter on, with school counts.

    Returns (subjects, designations, aid_types, employers). Derived from the
    matched schools rather than a fixed list, so a corpus carrying a subject the
    other one lacks - Chemistry appears only in the without-interview stream -
    still offers it.
    """
    subjects, designations, aid_types, employers = (Counter() for _ in range(4))
    for pref in dedupe_schools(prefs):
        rec, _ = lookup(idx, pref['code'], pref['level_code'], pref['medium'])
        if not rec:
            continue
        if rec['employer']:
            employers[rec['employer']] += 1
        for name, counter in (('subject_display', subjects),
                              ('designation_display', designations),
                              ('aid', aid_types)):
            for value in {v[name] for v in rec['vacancies'] if v[name]}:
                counter[value] += 1
    return subjects, designations, aid_types, employers


def dedupe_schools(prefs):
    """One entry per school+level+medium; the candidate's own subjects are kept
    as a comma-joined 'eligible as' note."""
    merged = {}
    for pref in prefs:
        key = (pref['code'], pref['level_code'], pref['medium'])
        if key in merged:
            merged[key]['eligible_as'].add(pref['subject_display'])
        else:
            entry = dict(pref)
            entry['eligible_as'] = {pref['subject_display']}
            merged[key] = entry
    for entry in merged.values():
        entry['eligible_as'] = ', '.join(sorted(entry['eligible_as']))
    return list(merged.values())


def analyse(prefs, idx, subjects=None, category='OPEN', designations=None,
            aid_types=None, employers=None, parallel=None):
    """Cross the candidate's eligible schools with the corpus.

    subjects / designations / aid_types / employers are display-name filters;
    None means no filter. `category` is a label from CATEGORY_ORDER. `parallel`
    is an optional list of PARALLEL_COLUMNS names to report alongside the
    category total.

    Every school from the preference PDF gets at least one row. Schools with no
    vacancy in the selected subjects come back with Match = 'No' and 0 posts, so
    nothing disappears from the list.
    """
    cat_no = CATEGORY_BY_LABEL[category]
    parallel = parallel or []
    subjects = set(subjects) if subjects else None
    designations = set(designations) if designations else None
    aid_types = set(aid_types) if aid_types else None
    employers = set(employers) if employers else None

    out = []
    for pref in dedupe_schools(prefs):
        rec, note = lookup(idx, pref['code'], pref['level_code'], pref['medium'])

        base = {
            'School Code': pref['code'],
            'School Name': pref['school_name'],
            'District': pref['district'],
            'Level': pref['level'],
            'Medium': pref['medium'],
            'Employer': rec['employer'] if rec else '',
            'Eligible As': pref['eligible_as'],
        }

        if rec is None:
            out.append({**base, 'Subject': '', 'Designation': '', 'Aid Type': '',
                        'Subject Posts': 0, 'Category': category,
                        'Category Posts': 0, 'School Total': 0,
                        'Category Share %': 0.0, 'Match': 'No',
                        **{p: 0 for p in parallel},
                        'Status': note})
            continue

        if employers is not None and rec['employer'] not in employers:
            continue

        roster = rec['reservation'].get(cat_no, [0] * 8)
        cat_posts = roster[PARALLEL_INDEX['TOTAL']]
        total = rec['school_total']
        share = round(100.0 * cat_posts / total, 1) if total else 0.0
        common = {
            **base,
            'Category': category,
            'Category Posts': cat_posts,
            'School Total': total,
            'Category Share %': share,
            **{p: roster[PARALLEL_INDEX[p]] for p in parallel},
            'Status': '; '.join([note] + rec['notes']).strip('; '),
        }

        matches = [
            v for v in rec['vacancies']
            if (subjects is None or v['subject_display'] in subjects)
            and (designations is None or v['designation_display'] in designations)
            and (aid_types is None or v['aid'] in aid_types)
        ]

        if not matches:
            out.append({**common, 'Subject': '', 'Designation': '', 'Aid Type': '',
                        'Subject Posts': 0, 'Match': 'No'})
            continue

        # Several rows can share a subject at different designations; keep them
        # separate so the candidate sees which level the post is at.
        grouped = defaultdict(int)
        for v in matches:
            grouped[(v['subject_display'], v['designation_display'], v['aid'])] += v['posts']
        for (subject, designation, aid), posts in sorted(grouped.items()):
            out.append({**common, 'Subject': subject, 'Designation': designation,
                        'Aid Type': aid, 'Subject Posts': posts, 'Match': 'Yes'})

    column_order = [
        'School Code', 'School Name', 'District', 'Level', 'Medium', 'Employer',
        'Subject', 'Designation', 'Aid Type', 'Subject Posts',
        'Category', 'Category Posts', *parallel,
        'School Total', 'Category Share %', 'Match', 'Eligible As', 'Status',
    ]
    return out, column_order


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli():
    args = sys.argv[1:]
    if not args or args[0] not in ('build', 'info'):
        print(__doc__)
        return

    if args[0] == 'build':
        which = args[1] if len(args) > 1 else 'all'
        if which not in CORPORA and which != 'all':
            print(f"Unknown corpus {which!r}. Use: all, {', '.join(CORPORA)}")
            return
        for key in (CORPORA if which == 'all' else [which]):
            cfg = CORPORA[key]
            if not os.path.isdir(cfg['folder']):
                print(f"[{cfg['label']}] SKIPPED - folder not found: {cfg['folder']}")
                continue
            print(f"\n[{cfg['label']}] parsing {cfg['folder']} ...")
            payload = build_cache(
                cfg['folder'], cfg['cache'],
                progress=lambda n, t: print(f'  {n}/{t}', end='\r', flush=True))
            print()
            _summarise(payload)
            print(f"  cache -> {cfg['cache']}")
        return

    for key, cfg in CORPORA.items():
        payload = load_cache(cfg['cache'])
        print(f"\n[{cfg['label']}]")
        if payload is None:
            print(f'  no cache yet. Run: python tait_engine.py build {key}')
            continue
        _summarise(payload)


def _summarise(payload):
    schools = payload['schools']
    posts = sum(s['school_total'] for s in schools)
    noted = [s for s in schools if s['notes']]
    print(f'  {len(schools)} advertisements parsed, {posts} posts total')
    print(f'  {len(payload["subjects"])} distinct subjects')
    levels = Counter(s['level'] for s in schools)
    print('  levels: ' + ', '.join(f'{k or "?"}={v}' for k, v in sorted(levels.items())))
    employers = Counter(s['employer'] for s in schools)
    print('  employers: ' + ', '.join(f'{k or "?"}={v}' for k, v in employers.most_common()))
    errors = [n for s in schools for n in s['notes'] if n.startswith('PARSE ERROR')]
    if errors:
        print(f'  *** {len(errors)} PARSE ERRORS ***')
    if noted:
        reasons = Counter(n.split(' ')[0] for s in noted for n in s['notes'])
        print(f'  {len(noted)} files carry a note: {dict(reasons)}')


if __name__ == '__main__':
    _cli()
