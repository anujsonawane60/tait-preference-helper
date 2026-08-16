"""TAIT School Preference Helper - Streamlit front end.

    python -m streamlit run app.py

Handles both recruitment streams. Upload the With Interview preference PDF, the
Without Interview one, or both; each produces its own ranked table and its own
CSV named after the candidate.

All parsing lives in tait_engine.py; this file is presentation only.
"""

import os

import pandas as pd
import streamlit as st

import tait_engine as E
import tait_report
from tait_engine import safe_filename

st.set_page_config(page_title='TAIT Preference Helper', page_icon='🏫', layout='wide')


@st.cache_resource(show_spinner=False)
def get_cache(key):
    """Load one corpus and its lookup index. Cached for the life of the server."""
    payload = E.load_cache(E.CORPORA[key]['cache'])
    return (payload, E.index_cache(payload)) if payload else (None, None)


# Both caches are bounded. With several people using the app all day an unbounded
# cache would keep every candidate's parsed PDF and every generated report in
# memory until the process was restarted.
@st.cache_data(show_spinner='Reading your PDF…', max_entries=16, ttl=3600)
def get_prefs(file_bytes):
    return E.parse_preference_pdf(file_bytes)


@st.cache_data(show_spinner='Building PDF…', max_entries=8, ttl=1800)
def build_pdf(df, category, label, person, parallel, disability):
    """Cached so a report is not rebuilt while the same table is on screen."""
    return tait_report.build_report(df, category, label, person=person,
                                    parallel=list(parallel), disability=disability)


def render_stream(key, upload, category, parallel, person, disability):
    """Render one recruitment stream: filters, table and download button.

    Widget keys carry both the stream and the uploaded file's id. The stream part
    keeps the two sections apart; the file part matters more. Streamlit stores a
    keyed widget's value in session_state and then IGNORES `default=` on later
    reruns, so reusing a fixed key would hand the next candidate the previous
    candidate's filters - which silently produced results for one candidate using
    another's post levels. A new upload means new keys, so the defaults apply.
    """
    cfg = E.CORPORA[key]
    cache, index = get_cache(key)
    wk = f'{key}_{getattr(upload, "file_id", "x")}'      # widget-key prefix

    prefs = get_prefs(upload.getvalue())
    if not prefs:
        st.error('No school rows found in that PDF. Is it the Generated Preferences file?')
        return

    schools = E.dedupe_schools(prefs)
    found = sum(1 for s in schools
                if E.lookup(index, s['code'], s['level_code'], s['medium'])[0])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Rows in your PDF', len(prefs))
    c2.metric('Distinct schools', len(schools))
    c3.metric('Districts', len({s['district'] for s in schools}))
    c4.metric('Matched in corpus', f'{found}/{len(schools)}')
    if found < len(schools):
        st.warning(f'{len(schools) - found} school(s) have no advertisement PDF in this '
                   'corpus. They stay in the results with a note in the Status column.')

    subject_counts, designation_counts, aid_counts, employer_counts = \
        E.available_options(prefs, index)

    own_subjects = sorted({p['subject_display'] for p in prefs} & set(subject_counts))
    own_designations = sorted({p['designation'] for p in prefs} & set(designation_counts))

    left, right = st.columns([3, 2])
    with left:
        chosen_subjects = st.multiselect(
            'Subject(s)', key=f'{wk}_sub',
            options=sorted(subject_counts, key=lambda s: -subject_counts[s]),
            default=own_subjects,
            format_func=lambda s: f'{s}  ({subject_counts[s]} schools)',
            help='Defaults to the subjects your own preference PDF lists.')
        chosen_designations = st.multiselect(
            'Post level', key=f'{wk}_desig',
            options=sorted(designation_counts, key=lambda d: -designation_counts[d]),
            default=own_designations,
            format_func=lambda d: f'{d}  ({designation_counts[d]} schools)',
            help='Defaults to the post levels your own preference PDF lists.')
    with right:
        chosen_aid = st.multiselect(
            'Aid type', key=f'{wk}_aid', options=sorted(aid_counts),
            default=sorted(aid_counts),
            format_func=lambda a: f'{a}  ({aid_counts[a]})')
        # Only worth showing where it varies - the with-interview corpus is all PA.
        if len(employer_counts) > 1:
            chosen_employers = st.multiselect(
                'Employer type', key=f'{wk}_emp', options=sorted(employer_counts),
                default=sorted(employer_counts),
                format_func=lambda e: f'{e}  ({employer_counts[e]})',
                help='ZP = zilla parishad, MP / MC = municipal, PA = private aided.')
        else:
            chosen_employers = None

    # Narrowing below what the candidate is actually eligible for is easy to do by
    # accident and impossible to see in the finished CSV, so say it out loud.
    dropped = []
    if set(chosen_subjects) < set(own_subjects):
        dropped.append('subject(s) ' + ', '.join(sorted(set(own_subjects) - set(chosen_subjects))))
    if set(chosen_designations) < set(own_designations):
        dropped.append('post level(s) ' + ', '.join(
            sorted(set(own_designations) - set(chosen_designations))))
    if dropped:
        st.warning(f'Your preference PDF also lists {" and ".join(dropped)}, which are '
                   'currently unticked — those schools will show 0 posts. Tick them back '
                   'on unless you meant to exclude them.', icon='⚠️')

    f1, f2 = st.columns(2)
    only_matches = f1.checkbox('Show only schools with a vacancy in my subject',
                               key=f'{wk}_only')
    only_disability = False
    if disability:
        only_disability = f2.checkbox(
            f'Show only schools with a {E.DISABILITY_LABEL[disability]} post',
            key=f'{wk}_onlydiv',
            help='Divyang posts are scarce — this can cut a long list down to a '
                 'handful of schools. Leave it off to keep the full list and sort '
                 'by the divyang column instead.')

    rows, columns = E.analyse(prefs, index,
                              subjects=chosen_subjects or None,
                              category=category,
                              designations=chosen_designations or None,
                              aid_types=chosen_aid or None,
                              employers=chosen_employers or None,
                              parallel=parallel,
                              disability=disability,
                              only_disability=only_disability)
    df = pd.DataFrame(rows, columns=columns)
    if only_matches:
        df = df[df['Match'] == 'Yes']
    if df.empty:
        st.info('Nothing matches these filters. Try widening the subject or level '
                'choice' + (', or untick the divyang-only filter — most schools '
                            'advertise no divyang post at all.' if only_disability
                            else '.'))
        return

    # 'Yes' before 'No', then the schools with the most posts in your category.
    # A divyang candidate is ranking on a much scarcer number, so when the quota
    # is on their own type outranks the category total.
    sort_by = ['Match']
    if disability:
        sort_by.append(f'Divyang {disability}')
    sort_by += ['Category Posts', 'Subject Posts', 'School Total']
    df = df.sort_values(sort_by, ascending=[False] * len(sort_by)).reset_index(drop=True)
    df.index += 1

    matched = df[df['Match'] == 'Yes']
    once = matched.drop_duplicates('School Code')
    tiles = [('Schools with your subject', matched['School Code'].nunique()),
             ('Posts in your subject', int(matched['Subject Posts'].sum())),
             (f'{category} posts at those schools', int(once['Category Posts'].sum()))]
    if 'OPEN Posts' in df.columns:
        tiles.append(('OPEN posts at those schools', int(once['OPEN Posts'].sum())))
    if disability:
        tiles.append((f'{E.DISABILITY_LABEL[disability]} posts',
                      int(once[f'Divyang {disability}'].sum())))
    for column, (caption, value) in zip(st.columns(len(tiles)), tiles):
        column.metric(caption, value)

    st.dataframe(df, width='stretch', height=460)

    tag = category.replace(' ', '').replace('/', '-')
    if disability:
        tag += f'_{disability}'
    suffix = cfg['label'].replace(' ', '')
    stem = f'{person}_TAIT_{tag}_{suffix}' if person else f'TAIT_{tag}_{suffix}'

    d1, d2 = st.columns(2)
    d1.download_button(
        f'⬇ CSV — {stem}.csv', df.to_csv(index=False).encode('utf-8-sig'),
        file_name=f'{stem}.csv', mime='text/csv', type='primary',
        width='stretch', key=f'{wk}_csv')

    # The PDF is built on request, not on every rerun. A 600-row report takes a
    # few seconds of pure-Python work, and building it each time a filter moved
    # would burn CPU that other people using the app at the same moment need.
    slot = d2.empty()
    pdf_warnings = []
    if st.session_state.get(f'{wk}_wantpdf'):
        pdf_bytes, pdf_warnings = build_pdf(
            df, category, cfg['label'], person, tuple(parallel), disability)
        slot.download_button(
            f'⬇ PDF — {stem}.pdf', pdf_bytes, file_name=f'{stem}.pdf',
            mime='application/pdf', width='stretch', key=f'{wk}_pdf')
    elif slot.button('📄 Prepare PDF', width='stretch', key=f'{wk}_mkpdf',
                     help='Takes a few seconds for a long list.'):
        st.session_state[f'{wk}_wantpdf'] = True
        st.rerun()
    for message in pdf_warnings:
        st.caption(f'ℹ️ {message}')
    st.caption('The CSV holds every column for sorting in Excel. The PDF is a printable '
               'summary — landscape, repeating headers, ready to work down while filling '
               'the portal.')


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------
st.title('🏫 TAIT School Preference Helper')
st.caption('Upload your Generated Preferences PDF, choose your subject and category, '
           'and see the vacancy and reservation position of every school on your list.')

with st.sidebar:
    st.header('Advertisement corpora')
    for key, cfg in E.CORPORA.items():
        cache, _ = get_cache(key)
        if cache:
            st.success(f"**{cfg['label']}** — {len(cache['schools'])} advertisements")
        else:
            st.error(f"**{cfg['label']}** — no cache")
        if st.button(f"Rebuild {cfg['label']}", key=f'rb_{key}', width='stretch'):
            if not os.path.isdir(cfg['folder']):
                st.error(f"Folder not found: {cfg['folder']}")
            else:
                bar = st.progress(0.0, text='Parsing PDFs...')
                E.build_cache(cfg['folder'], cfg['cache'],
                              progress=lambda n, t: bar.progress(n / t, text=f'{n}/{t} PDFs'))
                bar.empty()
                get_cache.clear()
                st.rerun()
    st.caption('Each corpus is parsed once and cached, so every analysis runs instantly. '
               'Rebuild only when the advertisement PDFs change.')

if not any(get_cache(k)[0] for k in E.CORPORA):
    st.warning('No corpus cache found. Run `python tait_engine.py build all` in a '
               'terminal, or use the sidebar buttons.')
    st.stop()

# Every per-candidate widget key carries this counter. Bumping it gives the whole
# form fresh widgets, which is the only reliable way to clear Streamlit's stored
# widget state - otherwise the next candidate inherits this one's name, category
# and uploads.
nonce = st.session_state.setdefault('candidate_nonce', 0)

head, reset = st.columns([5, 1])
head.subheader('1. Your details')
if reset.button('🔄 New candidate', width='stretch',
                help='Clear the name, category and uploaded PDFs before starting '
                     'the next person.'):
    st.session_state['candidate_nonce'] = nonce + 1
    st.rerun()

d1, d2, d3 = st.columns([2, 1, 2])
person = safe_filename(d1.text_input('Your name', placeholder='e.g. Paul Kulkarni',
                                     key=f'name_{nonce}',
                                     help='Used to name the downloaded files.'))
category = d2.selectbox('Your category', E.CATEGORY_ORDER,
                        index=E.CATEGORY_ORDER.index('OPEN'), key=f'cat_{nonce}',
                        format_func=lambda c: f'{c}  ({E.CATEGORIES[E.CATEGORY_BY_LABEL[c]][1]})')
parallel = d3.multiselect('Also show parallel reservation', E.PARALLEL_COLUMNS[:-1],
                          default=['Female'], key=f'par_{nonce}',
                          help='Parallel quotas are carved out of your category total, '
                               'not added to it. Each one is shown for your category '
                               'and for OPEN, side by side.')

# The divyang quota is off unless asked for: switching it on adds seven columns
# that mean nothing to the candidates who do not qualify for it.
p1, p2, p3 = st.columns([2, 1.4, 1.6])
use_pwd = p1.checkbox('I am applying under the divyang (PwD) quota',
                      key=f'pwd_{nonce}',
                      help='Adds each school\'s divyang posts, taken from the '
                           'दिव्यांग (4%) block the advertisement prints.')
disability = None
if use_pwd:
    disability = p2.selectbox(
        'Disability type', E.DISABILITY_CODES, key=f'dis_{nonce}',
        format_func=lambda c: f'{E.DISABILITY_LABEL[c]} ({E.DISABILITY_MARATHI[c]})')
    # The certificate percentage is the candidate's own and appears in no
    # advertisement PDF, so it gates eligibility and nothing else. It is never
    # used to compute a seat count - the roster prints the real figures, and
    # 4% of the school total disagrees with them on most schools.
    pct = p3.number_input('Certificate %', min_value=0, max_value=100, step=1,
                          value=E.BENCHMARK_DISABILITY_PCT, key=f'pct_{nonce}',
                          help='From your disability certificate. Used only to check '
                               'the 40% benchmark — it does not change any school’s '
                               'post count.')
    if pct < E.BENCHMARK_DISABILITY_PCT:
        st.warning(f'A certificate below {E.BENCHMARK_DISABILITY_PCT}% is not a benchmark '
                   'disability, so the divyang quota does not apply. Showing your normal '
                   f'{category} view instead — check your certificate if this looks wrong.',
                   icon='⚠️')
        disability = None
    else:
        st.caption(f'ℹ️ Divyang is a **parallel** reservation: these posts are carved out '
                   f'of the {category} and OPEN totals beside them, not added to them. The '
                   'counts are the ones each advertisement prints — they are not '
                   'calculated from the 4% share. This tool cannot confirm your '
                   'eligibility; only the certificate and the portal can.')

st.subheader('2. Your Generated Preferences PDF(s)')
st.caption('Upload either one, or both — each gets its own table and its own CSV.')
u1, u2 = st.columns(2)
uploads = {
    'with': u1.file_uploader('With Interview', type='pdf', key=f'up_with_{nonce}'),
    'without': u2.file_uploader('Without Interview', type='pdf', key=f'up_without_{nonce}'),
}

if not any(uploads.values()):
    st.info('Upload at least one preference PDF to continue.')
    st.stop()

st.info('**Subject Posts** and **Category Posts** come from two separate tables in the '
        'advertisement. The PDF publishes subject-wise vacancies and a school-wide '
        'reservation roster, but never states which category a *particular* subject post '
        'belongs to — so these two columns must be read side by side, not multiplied.',
        icon='ℹ️')

for key, upload in uploads.items():
    if not upload:
        continue
    cache, _ = get_cache(key)
    st.divider()
    st.subheader(f"📄 {E.CORPORA[key]['label']}")
    if not cache:
        st.error(f"No cache for this corpus yet — build it from the sidebar, or run "
                 f"`python tait_engine.py build {key}`.")
        continue
    render_stream(key, upload, category, parallel, person, disability)
