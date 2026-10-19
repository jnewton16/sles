"""Massachusetts instruments and complete histories for an explicit General Court."""
from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from utils import cache as cache_io

import csv
from datetime import date, datetime
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from scrape.http import make_session

BASE_URL = 'https://malegislature.gov'
REPO_ROOT = Path(__file__).resolve().parents[2]
DETAILS_HEADER = ['bill_number', 'session', 'bill_type', 'filed_by', 'presenter', 'all_petitioners',
                  'sponsor', 'cosponsors', 'title', 'status', 'city_town', 'summary', 'committees',
                  'bill_url', 'term', 'title_source', 'canonical_bill_number', 'index_label']
HISTORY_HEADER = ['bill_number', 'session', 'chamber', 'action_date', 'action', 'order', 'term', 'canonical_bill_number']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}', term):
        raise ValueError('MA term must be YYYY_YYYY, for example 2025_2026')
    start, end = map(int, term.split('_'))
    if start % 2 != 1 or end != start + 1 or end > date.today().year:
        raise ValueError('MA requires an odd-starting consecutive two-year term without future years')
    return (start - 1637) // 2


def select_session(html, term):
    court = parse_term(term)
    options = BeautifulSoup(html, 'lxml').select('[data-refinername=lawsgeneralcourt] input[data-refinertoken]')
    matches = list({o['data-refinertoken']: o for o in options
                    if re.match(str(court) + r'(st|nd|rd|th)\b', o.parent.get_text(strip=True))}.values())
    if len(matches) != 1:
        raise ValueError(f'MA court {court} not uniquely listed in source')
    return {'court': court, 'token': matches[0]['data-refinertoken'],
            'label': re.match(r'\d+(?:st|nd|rd|th)', matches[0].parent.get_text(strip=True))[0]}


def _number(value):
    return re.sub(r'[.\s]', '', value).upper()


def parse_listing(html, session):
    soup = BeautifulSoup(html, 'lxml')
    summary = soup.select_one('.searchResultSummary')
    match = re.fullmatch(r'Showing results (\d+) to (\d+) of (?:about )?(\d+) results\.',
                         summary.get_text(' ', strip=True).replace(',', '') if summary else '')
    if not match: raise ValueError('MA missing index counts')
    first, last, total = map(int, match.groups())
    records = []
    for row in soup.select('#searchTable tbody tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 4 or not cells[3].find('a', href=True):
            raise ValueError('MA malformed index row')
        label = ' '.join(cells[1].get_text(' ', strip=True).split())
        appendix = re.fullmatch(r'([HS]\.\d+), Appendix ([A-Z]+)', label)
        displayed = re.fullmatch(r'([HS]D?\.\d+)(?: [A-Z])?', label)
        if appendix: displayed = appendix
        if displayed is None: raise ValueError(f'MA unexpected index label: {label}')
        number = displayed[1] + (appendix[2] if appendix else '')
        link = cells[3].find('a', href=True)
        url = urljoin(BASE_URL, link['href'])
        if urlparse(url).path != f'/Bills/{session["court"]}/{_number(number)}':
            raise ValueError('MA index bill/session identity mismatch')
        records.append({'number': number, 'index_label': label, 'appendix':bool(appendix), 'filed_by': cells[2].get_text(' ', strip=True),
                        'title': link.get_text(' ', strip=True), 'url': url})
    if len(records) != last-first+1 or not 1 <= first <= last <= total:
        raise ValueError('MA index count mismatch')
    return records, first, last, total


def parse_header(html, record, session, term):
    soup = BeautifulSoup(html, 'lxml')
    heading = soup.find('h1')
    subtitle = heading.select_one('.subTitle') if heading else None
    if subtitle is None or not re.match(str(session['court']) + r'(?:st|nd|rd|th)\b', subtitle.get_text(strip=True)):
        raise ValueError(f'MA wrong court on bill page: {record["url"]}')
    subtitle.extract()
    match = re.fullmatch(r'(.+?)\s+([HS]D?\.?\d+)', heading.get_text(' ', strip=True))
    if not match: raise ValueError('MA missing bill identity')
    canonical = match[2]
    if _number(canonical) != _number(record['number']):
        # The official docket route resolves to its assigned bill, retaining the
        # docket URL. Preserve both source identities instead of inventing a bill.
        if not re.fullmatch(r'[HS]D\d+', _number(record['number'])):
            raise ValueError(f'MA wrong bill: expected {record["number"]}, received {canonical}')
        if canonical[0] != record['number'][0]: raise ValueError('MA cross-chamber docket mapping')
    fields = {}
    info = soup.select_one('dl.billInfo')
    if info is None: raise ValueError('MA missing bill information')
    for label in info.select('dt'):
        value = label.find_next_sibling('dd')
        if value is None: raise ValueError('MA malformed bill information')
        fields[label.get_text(strip=True).rstrip(':')] = value.get_text(' ', strip=True)
    title = soup.select_one('#contentContainer h2')
    summary = soup.select_one('#pinslip') or title
    title_text=title.get_text(' ',strip=True) if title else record['title']
    if not title_text: raise ValueError('MA missing title in both bill page and search index')
    committees = '; '.join(dict.fromkeys(a.get_text(' ', strip=True) for a in soup.select('a[href*="/Committees/Detail"]')))
    tabs = {button.get('id'): urljoin(BASE_URL, button['data-href']) for button in soup.select('button[data-href]')}
    prefix = f'/Bills/{session["court"]}/{_number(canonical)}/'
    for key in ('BillHistory-tab', 'Cosponsor-tab'):
        if key in tabs and not urlparse(tabs[key]).path.startswith(prefix):
            raise ValueError('MA tab identity mismatch')
    details = [record['number'], session['label'], match[1], record['filed_by'], fields.get('Presenter',''), '',
               fields.get('Sponsor',''), '', title_text, fields.get('Status',''),
               fields.get('City/Town',''), summary.get_text(' ',strip=True) if summary else '', committees,
               record['url'], term, 'bill_page' if title else 'search_index', canonical, record.get('index_label', record['number'])]
    return details, tabs


def parse_history(html, canonical):
    soup = BeautifulSoup(html, 'lxml')
    summary = soup.select_one('.searchResultSummary')
    match = re.fullmatch(r'Displaying\s+(?:(\d+) to (\d+) of )?(\d+) actions? for .+?\s+([HS]D?\.?\d+)',
                         summary.get_text(' ', strip=True).replace(',', '') if summary else '')
    if not match or _number(match[4]) != _number(canonical):
        raise ValueError('MA missing history count or wrong bill history')
    total = int(match[3]); first = int(match[1]) if match[1] else 1; last = int(match[2]) if match[2] else total
    rows = []
    for row in soup.select('#searchResults table tbody tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 3: raise ValueError('MA malformed history row')
        raw_date = cells[0].get_text(strip=True)
        # Some published committee-processing actions have no date.
        when = datetime.strptime(raw_date, '%m/%d/%Y').date().isoformat() if raw_date else ''
        action = cells[2].get_text(' ',strip=True)
        if not action: raise ValueError('MA empty history action')
        rows.append([cells[1].get_text(' ',strip=True), when, action])
    if len(rows) != last-first+1 or not 1 <= first <= last <= total:
        raise ValueError('MA history page count mismatch')
    return rows, first, last, total


def parse_people(html):
    soup = BeautifulSoup(html, 'lxml')
    heading = soup.select_one('h3.fnSingleTab')
    kind = heading.get_text(' ',strip=True) if heading else ''
    if kind not in ('Petitioners','Cosponsors','Cosponsors/Original Petitioners'): raise ValueError('MA wrong people tab')
    # The published people table is unpaginated. Fail if this source changes,
    # rather than quietly truncating the list.
    if soup.select('[onclick*="reloadAjaxContent"]'): raise ValueError('MA people table now paginates; update parser')
    table = heading.find_next('table')
    if table is None: raise ValueError('MA missing people table')
    names, originals = [], []
    for row in table.select('tbody tr'):
        cells = row.find_all('td',recursive=False)
        if len(cells) != 2 or not cells[0].get_text(strip=True): raise ValueError('MA malformed people row')
        original = cells[0].select_one('.asterisk') is not None
        for marker in cells[0].select('.sr-only, .asterisk'): marker.decompose()
        name = cells[0].get_text(' ',strip=True)
        if not name: raise ValueError('MA empty person name')
        names.append(name)
        if original: originals.append(name)
    if not names: raise ValueError('MA empty people table')
    return {'petitioners': '; '.join(names if kind == 'Petitioners' else originals),
            'cosponsors': '; '.join(names if kind != 'Petitioners' else [])}


def _fetch(http, url, form=None, ajax=False):
    headers = {'X-Requested-With':'XMLHttpRequest'} if ajax else {}
    r = http.post(url, data=form, headers=headers, timeout=60) if form is not None else http.get(url,headers=headers,timeout=60)
    r.raise_for_status(); time.sleep(1)
    return r.text


def _cached(path, fetch, force):
    return cache_io.read_text(path, encoding='utf-8') if cache_io.exists(path) and not force else fetch()


def _save(path, html):
    pending = path.with_suffix(path.suffix+'.tmp'); cache_io.write_text(pending, html,encoding='utf-8'); cache_io.replace(pending, path)


@scrape_run
def scrape(state, term, verbose=False, force_fetch=False):
    if state.upper() != 'MA': raise ValueError('Massachusetts scraper requires state MA')
    parse_term(term)
    folder = REPO_ROOT / '.data/MA/bill'
    outputs = [folder/f'MA_{name}_{term}.csv' for name in ('Bill_Details','Bill_Histories')]
    manifest = folder/f'.MA_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping MA {term}: completed outputs exist (use --force-fetch to refresh)'); return
    cache = folder/'.cache'/term; cache.mkdir(parents=True,exist_ok=True)
    details, histories, empty, aliases, appendices = [], [], [], {}, []
    unavailable_fields = []
    with make_session() as http:
        session = select_session(_fetch(http,BASE_URL+'/Bills/Search?SearchTerms=&Page=1'),term)
        records, seen, total, page = [], set(), None, 1
        while total is None or len(records)<total:
            form = {'SearchTerms':'','Page':page,'Refinements[lawsgeneralcourt]':session['token'],
                    'SortManagedProperty':'lawsbillnumber','Direction':'asc'}
            local = cache/f'index_{page}.html'
            html = _cached(local,lambda:_fetch(http,BASE_URL+'/Bills/Search',form),force_fetch)
            group, first, last, count = parse_listing(html,session)
            if first != len(records)+1 or (total is not None and count!=total): raise ValueError('MA index gap/count changed')
            for record in group:
                if record['number'] in seen: raise ValueError('MA duplicate indexed instrument')
                seen.add(record['number'])
            records.extend(group); total=count; _save(local,html)
            if page%10==0 or last==count: print(f'MA index: {last}/{count}',flush=True)
            page+=1
        for i,record in enumerate(records,1):
            if record['appendix']:
                appendices.append({'label':record['index_label'],'url':record['url']})
                continue  # Separately indexed governor's amendments, not bills.
            key = _number(record['number']); local = cache/f'{key}_history_1.html'
            html = _cached(local,lambda:_fetch(http,record['url']+'/BillHistory'),force_fetch)
            detail,tabs = parse_header(html,record,session,term); canonical=detail[-2]
            if _number(canonical)!=key: aliases[record['number']]=canonical
            actions=[]
            if 'BillHistory-tab' in tabs:
                rows,first,last,count=parse_history(html,canonical)
                if first!=1: raise ValueError('MA history does not start at one')
                actions.extend(rows); _save(local,html); page=2
                while len(actions)<count:
                    part=cache/f'{key}_history_{page}.html'
                    more=_cached(part,lambda:_fetch(http,tabs['BillHistory-tab']+f'?pageNumber={page}',ajax=True),force_fetch)
                    rows,first,last,n=parse_history(more,canonical)
                    if first!=len(actions)+1 or n!=count: raise ValueError('MA history pagination gap/count changed')
                    actions.extend(rows); _save(part,more); page+=1
            else:
                empty.append(record['number']); _save(local,html)
            if 'Cosponsor-tab' in tabs:
                part = cache / f'{key}_people.html'
                people = _cached(
                    part,
                    lambda: _fetch(http, tabs['Cosponsor-tab']),
                    force_fetch
                )

                people_detail, _ = parse_header(
                    people, record, session, term
                )

                if _number(people_detail[-2]) != _number(canonical):
                    raise ValueError('MA wrong people page identity')

                soup = BeautifulSoup(people, 'lxml')
                heading = soup.select_one('h3.fnSingleTab')
                table = soup.select_one('table')

                known_blank_tab = (
                    session['court'] == 194
                    and detail[2].strip().lower() == 'amendment'
                    and heading is None
                    and table is None
                )

                if known_blank_tab:
                    unavailable_fields.append({
                        'bill': record['number'],
                        'field': 'petitioners_and_cosponsors',
                        'reason': 'Advertised people tab contains no people table',
                        'url': tabs['Cosponsor-tab'],
                    })
                else:
                    try:
                        names = parse_people(people)
                    except ValueError as error:
                        print(
                            f"MA people-tab failure: {record['number']}",
                            flush=True
                        )
                        print(
                            f"People-tab URL: {tabs['Cosponsor-tab']}",
                            flush=True
                        )
                        raise

                    detail[5], detail[7] = (
                        names['petitioners'],
                        names['cosponsors']
                    )
                _save(part, people)
            details.append(detail)
            histories.extend([record['number'],session['label'],*row,j,term,canonical] for j,row in enumerate(actions,1))
            if verbose or i%25==0 or i==len(records): print(f'MA {i}/{len(records)} {record["number"]}: {len(actions)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged: cache_io.replace(pending, target)

        write_manifest(manifest, {'term':term,'court':session['court'],'details':len(details),'histories':len(histories),'source_empty_histories':empty,
                                      'unavailable_source_fields': unavailable_fields,'docket_bill_mappings':aliases,'excluded_appendices':appendices,
                                      'source_index_total':len(records)})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'MA {term}: saved {len(details):,} instruments and {len(histories):,} histories')
