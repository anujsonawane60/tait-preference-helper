"""Printable PDF report of a candidate's preference list.

Kept separate from tait_engine.py (parsing) and app.py (UI) so the report layout
can change without touching either.

Everything the report prints - school names, districts, subjects, category labels
- is Latin text in the source PDFs, so the built-in fonts cover the table. The
only field that can arrive in Devanagari is the candidate's own name, and
ReportLab does no complex-script shaping: it draws glyphs in codepoint order,
which renders Marathi matras in the wrong place. So a non-Latin name is shown
using an embedded Devanagari font only if one is available, and the caller is
told when it is not.
"""

import os
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

# Constants only - tait_engine has no UI dependency either, so this keeps the
# disability labels defined in exactly one place.
from tait_engine import DISABILITY_LABEL

BRAND = colors.HexColor('#1a4d7a')
BAND = colors.HexColor('#eef3f8')
RULE = colors.HexColor('#c3d3e2')
MUTED = colors.HexColor('#5b6b7b')

# Columns the report prints, in order: (dataframe column, heading, width in mm).
# Narrower than the CSV - a printed sheet is for scanning down while filling the
# portal. Post and Aid have to stay: one school can appear on several rows for the
# same subject at different levels or aid types, and without those two columns
# those rows read as duplicates carrying contradictory numbers.
LAYOUT = [
    ('#',              '#',        8),
    ('School Code',    'Code',    24),
    ('School Name',    'School',  60),
    ('District',       'District',24),
    ('Employer',       'Emp',     12),
    ('Designation',    'Post',    17),
    ('Subject',        'Subject', 34),
    ('Aid Type',       'Aid',     15),
    ('Subject Posts',  'Subj',    12),
]

# Printed after the quota block, which is assembled per report because its
# columns depend on the category, the parallel quotas ticked and whether the
# candidate is applying under the divyang quota.
TAIL = [('School Total', 'Total', 13)]

# Parallel-quota headings have to survive being prefixed with a category, so
# they are cut to two characters rather than the six a bare column got.
PARALLEL_ABBR = {
    'Female': 'F', 'Ex-Serviceman': 'Ex', 'Part-Time': 'PT',
    'Project-Affected': 'Pr', 'Earthquake-Affected': 'Eq', 'Sports': 'Sp',
    'Non-Parallel': 'NP',
}

# Column values are shortened so the printed row stays on one line.
SHORTEN = {
    'Designation': lambda t: (
        ('UGT ' if 'Under' in t else 'GT ') + re.search(r'\(([^)]*)\)', t).group(1)
        if re.search(r'\(([^)]*)\)', t) and 'Teacher' in t else t),
    'Aid Type': lambda t: (
        f'PA {t[len("PartialyAided"):]}' if t.startswith('PartialyAided') else t),
}

DISCLAIMER = (
    '<b>Subj</b> and the category columns come from two separate tables in each '
    'advertisement. The PDF publishes subject-wise vacancies and a school-wide '
    'reservation roster, but never states which category a particular subject post '
    'belongs to — read the columns side by side, do not combine them. '
    'The parallel quotas (female, sports, divyang) are carved <i>out of</i> the totals '
    'beside them, not added to them, and the divyang figures are the ones each PDF '
    'prints — they are not calculated from the 4% share. '
    'Figures are taken from the official advertisement PDFs; this is an information aid, '
    'it confirms no one’s eligibility and guarantees no posting.'
)


def _register_devanagari():
    """Register a Devanagari-capable font if Windows has one. Returns its name or ''."""
    for path, index in (('C:/Windows/Fonts/Nirmala.ttc', 0),
                        ('C:/Windows/Fonts/mangal.ttf', None)):
        if not os.path.exists(path):
            continue
        try:
            font = TTFont('Devanagari', path) if index is None else \
                TTFont('Devanagari', path, subfontIndex=index)
            pdfmetrics.registerFont(font)
            return 'Devanagari'
        except Exception:
            continue
    return ''


def _is_latin(text):
    return all(ord(c) < 0x0250 for c in text)


def build_report(df, category, stream_label, person='', parallel=(), disability=None):
    """Render the results table to PDF bytes.

    `df` is the dataframe already shown on screen, so the report always matches
    what the candidate is looking at. Returns (pdf_bytes, warnings).

    `disability` is a tait_engine disability code or None. The printed sheet
    carries only the candidate's own type and the divyang total; the CSV keeps
    all five buckets. A sheet meant to be worked down while filling the portal
    cannot afford five more columns without wrapping every row onto two lines.
    """
    warnings = []
    name_font = 'Helvetica-Bold'
    if person and not _is_latin(person):
        registered = _register_devanagari()
        if registered:
            name_font = registered
            warnings.append(
                'Your name is written in Devanagari. The PDF embeds a Marathi font, but '
                'joined letters and matras may not sit correctly — check the heading, and '
                'type your name in English if it looks wrong.')
        else:
            warnings.append(
                'No Devanagari font was found on this machine, so your name is left out '
                'of the PDF heading. The CSV filename still uses it.')
            person = ''

    # Headings name the block they belong to — 'OBC' / 'OBC-F' / 'OPEN' /
    # 'OPEN-F' — because a bare 'Female' next to two totals is exactly the
    # column a candidate reads against the wrong one.
    parallel = [p for p in parallel if f'Category {p}' in df.columns]
    short_cat = category.split('/')[0].strip()[:4]
    layout = list(LAYOUT)
    layout.append(('Category Posts', short_cat, 12))
    for p in parallel:
        layout.append((f'Category {p}', f'{short_cat}-{PARALLEL_ABBR.get(p, p[:2])}', 13))
    if 'OPEN Posts' in df.columns:          # absent when OPEN is the category
        layout.append(('OPEN Posts', 'OPEN', 12))
        for p in parallel:
            layout.append((f'OPEN {p}', f'OPEN-{PARALLEL_ABBR.get(p, p[:2])}', 13))
    if disability and f'Divyang {disability}' in df.columns:
        layout.append((f'Divyang {disability}', disability, 11))
        layout.append(('Divyang Total', 'Divy', 11))
    layout += TAIL

    styles = getSampleStyleSheet()
    cell = ParagraphStyle('cell', parent=styles['BodyText'], fontSize=7.2,
                          leading=8.6, spaceAfter=0)
    head = ParagraphStyle('head', parent=cell, fontSize=7.4, textColor=colors.white,
                          fontName='Helvetica-Bold')
    title = ParagraphStyle('title', parent=styles['Title'], fontSize=16,
                           textColor=BRAND, spaceAfter=2)
    sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=9,
                         textColor=MUTED, alignment=TA_CENTER, spaceAfter=1)
    note = ParagraphStyle('note', parent=styles['Normal'], fontSize=6.6,
                          textColor=MUTED, leading=8.2)

    matched = df[df['Match'] == 'Yes'] if 'Match' in df.columns else df
    schools = matched['School Code'].nunique() if len(matched) else 0
    subj_posts = int(matched['Subject Posts'].sum()) if len(matched) else 0
    cat_posts = (int(matched.drop_duplicates('School Code')['Category Posts'].sum())
                 if len(matched) else 0)

    heading = f'{stream_label} &nbsp;•&nbsp; Category: <b>{category}</b>'
    if disability:
        heading += f' &nbsp;•&nbsp; Divyang: <b>{DISABILITY_LABEL.get(disability, disability)}</b>'
    if person:
        heading += f' &nbsp;•&nbsp; {person}'

    story = [
        Paragraph('TAIT — School Preference List', title),
        Paragraph(heading, sub),
        Paragraph(f'{schools} schools with your subject &nbsp;•&nbsp; '
                  f'{subj_posts} posts in your subject &nbsp;•&nbsp; '
                  f'{cat_posts} {category} posts at those schools', sub),
        Spacer(1, 5 * mm),
    ]
    if disability:
        col = f'Divyang {disability}'
        div_schools = int((matched.drop_duplicates('School Code')[col] > 0).sum()) \
            if len(matched) and col in matched.columns else 0
        div_posts = int(matched.drop_duplicates('School Code')[col].sum()) \
            if len(matched) and col in matched.columns else 0
        story.insert(3, Paragraph(
            f'{div_posts} {DISABILITY_LABEL.get(disability, disability)} post(s) '
            f'across {div_schools} of those schools', sub))

    body = [[Paragraph(h, head) for _, h, _ in layout]]
    for n, (_, row) in enumerate(df.iterrows(), 1):
        line = []
        for col, _, _ in layout:
            if col == '#':
                line.append(Paragraph(str(n), cell))
                continue
            value = row.get(col, '')
            text = '' if value is None else str(value)
            if col in SHORTEN and text:
                text = SHORTEN[col](text)
            if col == 'School Name' and len(text) > 52:
                text = text[:50] + '…'
            line.append(Paragraph(text.replace('&', '&amp;'), cell))
        body.append(line)

    # Scale to the printable width rather than trusting the mm figures - picking
    # several parallel-quota columns would otherwise push the table off the page.
    page_width = landscape(A4)[0]
    available = page_width - 16 * mm
    widths = [w * mm for _, _, w in layout]
    if sum(widths) > available:
        factor = available / sum(widths)
        widths = [w * factor for w in widths]

    table = Table(body, colWidths=widths, repeatRows=1, hAlign='CENTER')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BRAND),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.25, RULE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, BAND]),
        ('LEFTPADDING', (0, 0), (-1, -1), 2.5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2.5),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story += [table, Spacer(1, 4 * mm), Paragraph(DISCLAIMER, note)]

    page_size = landscape(A4)
    doc = BaseDocTemplate(None, pagesize=page_size,
                          leftMargin=8 * mm, rightMargin=8 * mm,
                          topMargin=10 * mm, bottomMargin=12 * mm,
                          title=f'TAIT Preferences — {category}', author=person or 'TAIT')

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(8 * mm, 6 * mm, f'{stream_label} — {category}')
        canvas.drawRightString(page_size[0] - 8 * mm, 6 * mm, f'Page {document.page}')
        canvas.setStrokeColor(RULE)
        canvas.line(8 * mm, 9 * mm, page_size[0] - 8 * mm, 9 * mm)
        canvas.restoreState()

    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='body')
    doc.addPageTemplates([PageTemplate(id='all', frames=[frame], onPage=decorate)])

    import io
    buffer = io.BytesIO()
    doc.filename = buffer
    doc.build(story)
    return buffer.getvalue(), warnings
