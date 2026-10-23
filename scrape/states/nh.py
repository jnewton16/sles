"""New Hampshire's counted legacy search, bill status, text and dockets."""
from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from utils import cache as cache_io
import csv
from datetime import date, datetime
from pathlib import Path
import re
import time
from urllib.parse import urljoin,urlparse,parse_qs
from bs4 import BeautifulSoup
from scrape.http import make_session

BASE_URL='https://gc.nh.gov/bill_status/legacy/bs2016/'
REPO_ROOT=Path(__file__).resolve().parents[2]
DETAILS_HEADER=['bill_number','session_year','sponsors','sponsors_on_bill','title','lsr','local_gov','chapter_num','S_status','S_floor_date','S_comm','H_status','H_floor_date','H_comm','bill_url','hist_url','vote_url','term','source_bill_label']
HISTORY_HEADER=['bill_number','session_year','chamber','action_date','action','order','term','source_lsr']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}',term):raise ValueError('NH term must be YYYY_YYYY')
    start,end=map(int,term.split('_'))
    if start%2!=1 or end!=start+1 or start<1989 or end>date.today().year:raise ValueError('NH requires an odd-starting consecutive term from 1989 without future years')
    return start,end


def search_form(html,year):
    soup=BeautifulSoup(html,'lxml')
    if soup.select_one('input[name=txtsessionyear]') is None:raise ValueError('NH missing search form')
    form={n['name']:n.get('value','') for n in soup.select('input[name]') if n.get('type','text').lower() in ('hidden','text')}
    for n in soup.select('select[name]'):
        o=n.select_one('option[selected]') or n.find('option');form[n['name']]=o.get('value','') if o else ''
    form.update({'txtsessionyear':str(year),'ddlobody':'','sortoption':'billnumber','cmdsubmit':'Submit'})
    return form



def parse_listing(html, year):
    soup = BeautifulSoup(html, 'lxml')
    text = soup.get_text(' ', strip=True)
    count = re.search(r'Bills Found\s*:\s*(\d+)', text)
    if not count:
        raise ValueError('NH response is not a counted result list')
    records = []
    seen = set()
    for body in soup.select('.bill-body'):
        ident = body.select_one('.bill-ident')
        heading = ident.select_one('h3') if ident else None
        session = ident.select_one('.session-year') if ident else None
        detail = body.select_one('.bill-detail')
        if heading is None or session is None or detail is None:
            raise ValueError('NH incomplete bill index record')
        source_label = re.sub(r'\s+', '', heading.get_text())
        match = re.fullmatch(
            r'([A-Z]+\d+)(?:-(?:FN|A|L))*',
            source_label
        )
        if not match:
            raise ValueError('NH unknown bill number: ' + source_label)
        number = match.group(1)
        if session.get_text(' ', strip=True) != f'Session Year {year}':
            raise ValueError('NH index identity/year mismatch')
        links = {}
        for link in ident.select('a[href]'):
            url = urljoin(BASE_URL, link['href'])
            parts = urlparse(url)
            path = parts.path.lower()
            params = parse_qs(parts.query)
            if any(p in path for p in (
                'bill_docket.aspx',
                'bill_status.aspx',
                'billtext.aspx'
            )):
                if params.get('sy') != [str(year)]:
                    raise ValueError('NH link year mismatch')
            if 'bill_docket.aspx' in path:
                links['docket'] = url
                links['lsr'] = params['lsr'][0]
            elif 'bill_status.aspx' in path:
                links['status'] = url
            elif (
                'billtext.aspx' in path
                and params.get('txtFormat') == ['html']
            ):
                links['text'] = url
            elif 'roll' in path:
                links['vote'] = url
        if not {'docket', 'status', 'text', 'lsr'} <= links.keys():
            raise ValueError('NH missing record links: ' + number)
        if heading.get('id') != f"bill-{year}-{links['lsr']}":
            raise ValueError('NH heading/LSR mismatch: ' + number)
        if links['lsr'] in seen:
            raise ValueError('NH duplicate source identifier')
        seen.add(links['lsr'])
        title_node = next(
            (
                p for p in detail.select('p')
                if p.find('strong')
                and p.find('strong').get_text(' ', strip=True) == 'Title:'
            ),
            None
        )
        if title_node is None:
            raise ValueError('NH missing index title: ' + number)
        title = title_node.get_text(' ', strip=True)
        title = title.removeprefix('Title:').strip()
        if not title:
            raise ValueError('NH empty index title: ' + number)
        records.append({
            'number': number,
            'year': year,
            'title': title,
            'source_label': source_label,
            **links
        })
    if not records or len(records) != int(count.group(1)):
        raise ValueError('NH incomplete/empty result list')
    return records


def _date(value):
    return datetime.strptime(value,'%m/%d/%Y').date().isoformat() if value else ''


def _guard(soup,record,docket=False):
    # The bill's own reciprocal status/docket link carries both session and LSR.
    target='bill_status.aspx' if docket else 'bill_docket.aspx'
    matches=[]
    for a in soup.select('a[href]'):
        if target in a['href'].lower():
            p=parse_qs(urlparse(a['href']).query)
            matches.append(p.get('sy')==[str(record['year'])] and int(p.get('lsr',['-1'])[0])==int(record['lsr']))
    if not matches or not all(matches):raise ValueError('NH detail session/LSR mismatch')
    text=soup.get_text(' ',strip=True)
    if not re.search(r'\b'+re.escape(record['number'])+r'\b',text):raise ValueError('NH detail bill number mismatch')

def is_housekeeping_notice(body, record):
    if record['year'] != 2025:
        return False

    match = re.fullmatch(r'SR(\d+)', record['number'])
    if match is None:
        return False

    number = int(match.group(1))

    if 1 <= number <= 5:
        expected = (
            'Senate Resolutions 1-5 were housekeeping resolutions '
            'adopted during the 2025 Legislative Session, and are '
            'recorded in the 2025 Senate Journals.'
        )
    elif 6 <= number <= 8:
        expected = (
            f'Senate Resolution {number} was a housekeeping resolution '
            'adopted during the 2025 Legislative Session, and is '
            'recorded in the 2025 Senate Journals.'
        )
    else:
        return False

    return expected in body

def parse_bill(html,docket,bill_text,record,term):
    soup=BeautifulSoup(html,'lxml');_guard(soup,record)
    status_strip = soup.select_one('dl.status-strip')
    if status_strip is None:
        raise ValueError('NH missing general status section')
    general_fields = {}
    for dt in status_strip.select('dt'):
        dd = dt.find_next_sibling('dd')
        if dd is None:
            raise ValueError('NH missing general status field value')
        label = dt.get_text(' ', strip=True)
        if label in general_fields:
            raise ValueError('NH duplicate general status field: ' + label)
        general_fields[label] = dd.get_text(' ', strip=True)
    required = {'LSR #', 'Body', 'Local Govt', 'Chapter #', 'Gen Status'}
    if not required <= general_fields.keys():
        raise ValueError('NH incomplete general status fields')
    if int(general_fields['LSR #']) != int(record['lsr']):
        raise ValueError('NH general status LSR mismatch')
    def flag(label):
        return general_fields[label]
    sponsor_list = soup.select_one('ul.sponsor-list')
    sponsor_section = soup.select_one('section[aria-labelledby="sponsors"]')
    explicitly_empty = (
        sponsor_section is not None
        and sponsor_section.select_one('p') is not None
        and sponsor_section.select_one('p').get_text(' ', strip=True)
        == 'No sponsors are recorded for this bill.'
    )
    if sponsor_list is None:
        if not explicitly_empty:
            raise ValueError('NH missing sponsors list')
        sponsors = ''
    else:
        sponsor_items = sponsor_list.select('li.sponsor')
        if not sponsor_items:
            raise ValueError('NH empty sponsors list')
        if explicitly_empty:
            raise ValueError('NH contradictory sponsor information')
        sponsors = '; '.join(
            ' '.join(
                item.get_text(' ', strip=True)
                .replace('\xa0', ' ').split()
            )
            for item in sponsor_items
        )
    source_text = BeautifulSoup(bill_text, 'lxml')
    body = source_text.get_text(' ', strip=True)

    document_title = (
        source_text.title.get_text(' ', strip=True)
        if source_text.title else ''
    )

    if not re.search(
        rf'\b{re.escape(record["number"])}\b',
        document_title,
        re.I
    ):
        raise ValueError('NH bill text identity mismatch')

    housekeeping_notice = is_housekeeping_notice(body, record)

    resolution_match = re.fullmatch(
        r'HR(\d+)',
        record['number']
    )

    if housekeeping_notice:
        pass

    elif resolution_match:
        expected_number = int(resolution_match.group(1))

        if not re.search(
            rf'\bHOUSE\s+RESOLUTION\s+(?:NO\.\s*)?{expected_number}\b',
            body,
            re.I
        ):
            raise ValueError('NH House resolution heading mismatch')

        text_params = parse_qs(
            urlparse(record['text']).query
        )

        if text_params.get('sy') != [str(record['year'])]:
            raise ValueError('NH House resolution text year mismatch')

        document_ids = re.findall(
            r'\b(\d{2})-(\d{4})\b',
            body
        )

        if document_ids:

            term_start = int(term.split('_')[0])
            allowed_prefixes = {
                str(term_start)[-2:],
                str(term_start + 1)[-2:],
            }

            if not any(
                prefix in allowed_prefixes
                and int(number) == int(record['lsr'])
                for prefix, number in document_ids
            ):
                raise ValueError('NH House resolution LSR mismatch')

    else:
        session_match = re.search(
            r'\b(20\d{2})\s+SESSION\b',
            body,
            re.I
        )

        if session_match is None:
            raise ValueError('NH missing bill text session')

        term_start = int(term.split('_')[0])
        printed_year = int(session_match.group(1))

        if printed_year not in (term_start, term_start + 1):
            raise ValueError('NH bill text session outside term')

        document_ids = re.findall(
            r'\b(\d{2})-(\d{4})\b',
            body
        )

        allowed_prefixes = {
            str(term_start)[-2:],
            str(term_start + 1)[-2:],
        }

        if not any(
            prefix in allowed_prefixes
            and int(number) == int(record['lsr'])
            for prefix, number in document_ids
        ):
            raise ValueError('NH bill text LSR mismatch')

    sponsor_line=next((n for n in source_text.select('p') if re.match(r'(?:SPONSORS?|INTRODUCED BY)\s*:',n.get_text(' ',strip=True),re.I)),None)
    on_bill=sponsor_line.get_text(' ',strip=True) if sponsor_line else ''
    info = {}
    for chamber, css_class in (
        ('House Status', 'chamber--house'),
        ('Senate Status', 'chamber--senate'),
    ):
        section = soup.select_one(f'section.{css_class}')
        if section is None:
            raise ValueError(f'NH missing {chamber} section')
        heading = section.select_one('h2')
        if heading is None or heading.get_text(' ', strip=True) != chamber:
            raise ValueError(f'NH incorrect {chamber} heading')
        fields = {}
        for dt in section.select('dl > dt'):
            dd = dt.find_next_sibling('dd')
            if dd is None:
                raise ValueError(f'NH missing {chamber} field value')
            label = dt.get_text(' ', strip=True)
            if label in fields:
                raise ValueError(f'NH duplicate {chamber} field: {label}')
            fields[label] = dd.get_text(' ', strip=True)
        required = {'Status', 'Floor Date', 'Current Committee'}
        if not required <= fields.keys():
            raise ValueError(f'NH incomplete {chamber} fields')
        info[chamber] = [
            fields['Status'],
            _date(fields['Floor Date']),
            fields['Current Committee'],
        ]
    detail=[record['number'],record['year'],sponsors,on_bill,record['title'],record['lsr'],flag('Local Govt'),flag('Chapter #'),*info['Senate Status'],*info['House Status'],record['status'],record['docket'],record.get('vote',''),term,record['source_label']]
    source=BeautifulSoup(docket,'lxml');_guard(source,record,True)
    table = source.select_one('table.docket-table')
    empty_docket = (
        table is None
        and 'No docket entries are recorded for this bill.'
        in source.get_text(' ', strip=True)
    )
    if table is None and not empty_docket:
        raise ValueError('NH missing docket table')
    if empty_docket:
        return detail, []
    rows = table.find_all('tr')
    if not rows:
        raise ValueError('NH empty docket table')
    header = [
        cell.get_text(' ', strip=True)
        for cell in rows[0].find_all(['th', 'td'], recursive=False)
    ]
    if header != ['Date', 'Body', 'Description']:
        raise ValueError('NH unexpected docket columns')
    history = []
    for row in rows[1:]:
        cells = row.find_all('td', recursive=False)
        if not cells:
            continue
        if len(cells) != 3:
            raise ValueError('NH malformed docket row')
        cleaned = []
        for cell in cells:
            cell_soup = BeautifulSoup(str(cell), 'lxml')
            for hidden in cell_soup.select('.visually-hidden'):
                hidden.decompose()
            cleaned.append(
                ' '.join(cell_soup.get_text(' ', strip=True).split())
            )
        raw_date, chamber, action = cleaned
        if chamber not in ('H', 'S'):
            raise ValueError('NH unexpected docket chamber')
        if not action:
            raise ValueError('NH empty action')
        history.append([
            record['number'],
            record['year'],
            chamber,
            _date(raw_date),
            action,
            len(history) + 1,
            term,
            record['lsr']
        ])
    if not history:
        raise ValueError('NH empty docket')
    return detail,history


def _save(path,html):
    tmp=path.with_suffix('.html.tmp');cache_io.write_text(tmp, html,encoding='utf-8');cache_io.replace(tmp, path)


def _fetch(http,url,form=None):
    r=http.get(url,timeout=60) if form is None else http.post(url,data=form,timeout=90)
    r.raise_for_status();time.sleep(1);return r.text


def repair_docket(http, page, status, record):
    """Recover the source's embedded metadata error through its own bill link."""
    if 'Advanced_BillStatus.Bill_Docket.getdata()' not in page:return page
    soup=BeautifulSoup(status,'lxml')
    links=[urljoin(BASE_URL,a['href']) for a in soup.select('a[href]')
           if a.get_text(' ',strip=True)=='Bill Docket' and 'bill_docket.aspx' in a['href'].lower()]
    if len(set(links))!=1:raise ValueError('NH missing unique reciprocal docket link')
    url=links[0];params=parse_qs(urlparse(url).query)
    if params.get('sy')!=[str(record['year'])] or int(params.get('lsr',['-1'])[0])!=int(record['lsr']):
        raise ValueError('NH reciprocal docket identity mismatch')
    for candidate in (url,url.replace('https://gc.nh.gov/','https://www.gencourt.state.nh.us/')):
        fresh=_fetch(http,candidate)
        if 'Advanced_BillStatus.Bill_Docket.getdata()' in fresh:continue
        _guard(BeautifulSoup(fresh,'lxml'),record,True)
        return fresh
    raise ValueError('NH docket metadata error persists on reciprocal and alternate-host routes')


@scrape_run
def scrape(state,term,verbose=False,force_fetch=False):
    if state.upper()!='NH':raise ValueError('New Hampshire scraper requires state NH')
    years=parse_term(term);folder=REPO_ROOT/'.data/NH/bill';cache=folder/'.cache'/term;cache.mkdir(parents=True,exist_ok=True)
    outputs=[folder/f'NH_{kind}_{term}.csv' for kind in ('Bill_Details','Bill_Histories')];manifest=folder/f'.NH_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping NH {term}: completed outputs exist (use --force-fetch to refresh)');return
    details,histories,counts=[],[],{}
    empty_sponsors = []
    missing_text = []
    empty_histories = []
    with make_session() as http:
        for year in years:
            index=cache/f'{year}_index.html'
            if cache_io.exists(index) and not force_fetch:html=cache_io.read_text(index)
            else:
                home = _fetch(http, BASE_URL)
                form = search_form(home, year)
                html = _fetch(http, urljoin(BASE_URL, 'results.aspx'), form)
            records=parse_listing(html,year);_save(index,html);counts[str(year)]=len(records);print(f'NH {year}: {len(records)} instruments',flush=True)
            for i,record in enumerate(records,1):
                pages=[]
                for kind in ('status','docket','text'):
                    local=cache/f'{year}_{record["lsr"]}_{kind}.html'
                    page=cache_io.read_text(local) if cache_io.exists(local) and not force_fetch else _fetch(http,record[kind])
                    if kind=='docket':page=repair_docket(http,page,pages[0],record)
                    _save(local,page);pages.append(page)
                try:
                    d, h = parse_bill(*pages, record, term)
                except Exception:
                    print(
                        f"NH bill parsing failure: "
                        f"{record['number']} "
                        f"(year={year}, LSR={record['lsr']})",
                        flush=True
                    )
                    raise
                if not d[2]:
                    empty_sponsors.append({
                        'bill': record['number'],
                        'year': year,
                        'lsr': record['lsr'],
                        'url': record['status'],
                        'reason': 'Source explicitly records no sponsors',
                    })
                text_body = BeautifulSoup(
                    pages[2], 'lxml'
                ).get_text(' ', strip=True)

                verified_housekeeping_notice = is_housekeeping_notice(
                    text_body, record
                )

                if verified_housekeeping_notice:
                    missing_text.append({
                        'bill': record['number'],
                        'year': year,
                        'lsr': record['lsr'],
                        'url': record['text'],
                        'reason': 'Official text page contains housekeeping notice instead of resolution text',
                    })
                if not h:
                    empty_histories.append({
                        'bill': record['number'],
                        'year': year,
                        'lsr': record['lsr'],
                        'url': record['docket'],
                        'reason': 'Source explicitly records no docket entries',
                    })
                details.append(d)
                histories.extend(h)
                if verbose or i%25==0 or i==len(records):print(f'NH {year} {i}/{len(records)} {record["number"]}: {len(h)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged:cache_io.replace(pending, target)
        write_manifest(manifest, {'term': term,'counts': counts,'details': len(details),'histories': len(histories),'source_empty_sponsors': empty_sponsors,'source_missing_text': missing_text,'source_empty_histories': empty_histories,})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'NH {term}: wrote {len(details)} details and {len(histories)} histories',flush=True)
