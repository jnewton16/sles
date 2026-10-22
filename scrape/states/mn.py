"""Minnesota term-selected bills, authors, text summaries and chamber histories."""
from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from utils import cache as cache_io

import csv
from datetime import date, datetime
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from scrape.http import make_session

BASE_URL='https://www.revisor.mn.gov'
SEARCH_URL=BASE_URL+'/bills/status_search.php?search=advanced'
REPO_ROOT=Path(__file__).resolve().parents[2]
DETAILS_HEADER=['bill_number','session','author','coauthors','outchamber_authors','companion','description','summary','bill_url','term','text_url']
HISTORY_HEADER=['bill_number','session','chamber','action_date','action','journal_page','action_note','chamber_order','term','date_source']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}',term):raise ValueError('MN term must be YYYY_YYYY')
    start,end=map(int,term.split('_'))
    if start%2!=1 or end!=start+1 or end>date.today().year:raise ValueError('MN requires an odd-starting consecutive two-year term without future years')
    return start,end


def select_sessions(html,term):
    start,end=parse_term(term);records=[]
    for option in BeautifulSoup(html,'lxml').select('#session option[value]'):
        label=option.get_text(' ',strip=True)
        m=re.fullmatch(r'(\d+)(?:st|nd|rd|th) Legislature, (\d{4})(?:-(\d{4})| (\d+)(?:st|nd|rd|th) Special Session)',label)
        if not m:raise ValueError(f'MN unrecognized session label: {label}')
        if m[3] and (int(m[2]),int(m[3]))==(start,end):
            records.append({'id':option['value'],'legislature':int(m[1]),'year':start,'special':0,'short':f'{start}_RS','label':label})
        elif m[4] and int(m[2]) in (start,end):
            records.append({'id':option['value'],'legislature':int(m[1]),'year':int(m[2]),'special':int(m[4]),'short':f'{m[2]}_S{m[4]}','label':label})
    if sum(r['special']==0 for r in records)!=1:raise ValueError('MN requested regular session not uniquely listed')
    if len({r['id'] for r in records})!=len(records):raise ValueError('MN duplicate session')
    return sorted(records,key=lambda r:(r['year'],r['special']))


def _identity(url,session):
    m=re.fullmatch(r'/bills/(\d+)/(\d{4})/(\d+)/([A-Z]+)/(\d+)/?',urlparse(url).path)
    if not m or int(m[1])!=session['legislature'] or int(m[3])!=session['special']:
        raise ValueError(f'MN wrong bill/session URL: {url}')
    allowed={session['year']} if session['special'] else {session['year'],session['year']+1}
    if int(m[2]) not in allowed:raise ValueError('MN bill URL outside session years')
    return m[4]+str(int(m[5])).zfill(4)


def parse_listing(html,session,chamber):
    soup=BeautifulSoup(html,'lxml');heading=next((h.get_text(' ',strip=True) for h in soup.select('h3') if 'Documents Found' in h.get_text()),'')
    m=re.match(r'(?:([\d,]+) to ([\d,]+) of )?([\d,]+) (?:House |Senate )?Documents Found',heading)
    if not m:raise ValueError('MN missing result count')
    total=int(m[3].replace(',',''));first=int(m[1].replace(',','')) if m[1] else 1;last=int(m[2].replace(',','')) if m[2] else total
    if session['special']:
        if f'{session["year"]}, {session["special"]}' not in heading or 'Special Session' not in heading:raise ValueError('MN wrong special-session results')
    elif f'Legislative Session {session["legislature"]} ({session["year"]}-{session["year"]+1})' not in heading:raise ValueError('MN wrong regular-session results')
    table=soup.select_one('table.table-responsive')
    if table is None:raise ValueError('MN missing result table')
    records=[];count=0
    for row in table.select('tr'):
        cells=row.find_all('td',recursive=False)
        if not cells:continue
        if len(cells)!=8:raise ValueError('MN malformed index row')
        count+=1;link=cells[1].find('a',href=True)
        if link is None:raise ValueError('MN missing bill link')
        number=link.get_text(strip=True);url=urljoin(BASE_URL,link['href'])
        if _identity(url,session)!=number:raise ValueError('MN bill index identity mismatch')
        if number[0]!=chamber[0]:continue  # Transmitted opposite-chamber bills also appear.
        if parse_qs(urlparse(url).query).get('body')!=[chamber]:raise ValueError('MN index uses wrong originating chamber')
        records.append({'number':number,'url':url,'author':cells[6].get_text(' ',strip=True),'description':cells[7].get_text(' ',strip=True),
                        'official_actions':int(cells[2].get_text(strip=True))})
    if count!=last-first+1 or not 1<=first<=last<=total:raise ValueError('MN index row count mismatch')
    nexts={urljoin(BASE_URL+'/bills/',a['href']) for a in soup.select('a.next[href]')}
    if len(nexts)>1 or bool(nexts)!=(last<total):raise ValueError('MN missing/ambiguous pagination link')
    return records,first,last,total,next(iter(nexts),None)


def parse_bill(html,record,session,term):
    soup=BeautifulSoup(html,'lxml');heading=soup.select_one('h1.card-title')
    if heading is None or re.sub(r'\s+','',heading.get_text())!=re.sub(r'([A-Z]+)0+(\d)',r'\1\2',record['number']):
        raise ValueError('MN bill identity mismatch')
    header=heading.parent.get_text(' ',strip=True)
    if session['special']:
        if f'{session["year"]} {session["special"]}' not in header or 'Special Session' not in header:raise ValueError('MN wrong special session page')
    elif f'({session["year"]} - {session["year"]+1})' not in header:raise ValueError('MN wrong regular session page')
    root=soup.select_one('.house_bill,.senate_bill')
    if root is None:raise ValueError('MN missing bill content')
    author_groups={}
    for group in root.select('div.author'):
        label=group.find_previous(['h2','h3']).get_text(' ',strip=True)
        names=[a.get_text(' ',strip=True) for a in group.select('a')]
        n=re.search(r'\((\d+)\)',label)
        if n and int(n[1])!=len(names):raise ValueError('MN author count mismatch')
        key='out' if re.match(r'(House|Senate) Authors',label) else 'origin'
        if key in author_groups:raise ValueError('MN duplicate author section')
        author_groups[key]='; '.join(names)
    if 'origin' not in author_groups:raise ValueError('MN missing originating authors')
    companion_node=root.find(string=re.compile(r'Companion:'))
    if companion_node is None:raise ValueError('MN missing companion field')
    companion_links=[a for a in companion_node.parent.select('a[href]') if re.fullmatch(r'[HS][A-Z]+\s*\d+',a.get_text(strip=True))]
    companion='; '.join(a.get_text(strip=True) for a in companion_links)
    if not companion_links and 'None' not in companion_node.parent.get_text():raise ValueError('MN ambiguous companion field')
    desc_heading=root.find('h2',string='Description');desc=desc_heading.find_next_sibling('p') if desc_heading else None
    if desc is None:raise ValueError('MN missing description')
    texts=[a for a in root.select('a[href]') if '/versions/' in a['href'] and '/pdf' not in a['href']]
    text_url=urljoin(record['url'],texts[0]['href']) if texts else ''
    if text_url:
        path=urlparse(text_url).path.split('/versions/')[0]
        if _identity(path,session)!=record['number']:raise ValueError('MN wrong bill text link')
    detail=[record['number'],session['short'],record['author'],author_groups['origin'],author_groups.get('out',''),
            companion,desc.get_text(' ',strip=True),'',record['url'],term,text_url]
    histories=[];pane=root.select_one('#separated-tab-pane')
    if pane is None:raise ValueError('MN missing separated history view')
    for chamber in ('House','Senate'):
        rows=pane.select(f'div.{chamber.lower()} table.actions tr');previous=''
        for order,row in enumerate(rows,1):
            cells=row.find_all('td',recursive=False)
            if len(cells)!=2:raise ValueError('MN malformed action row')
            action=cells[1].select_one('.row > .col:not(.action_item)');notes=cells[1].select_one('.action_item')
            if action is None or notes is None:raise ValueError('MN missing action/notes layout')
            text=action.get_text(' ',strip=True);raw=cells[0].get_text(strip=True);date_source='date_column'
            if not raw:
                dates=re.findall(r'\b\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\b',text)
                if dates:raw=dates[-1];date_source='action_text'
                else:date_source='preceding_action'
            if raw:
                when=datetime.strptime(raw,'%m/%d/%Y' if len(raw.rsplit('/',1)[-1])==4 else '%m/%d/%y').date().isoformat()
            else:when=previous
            if not when or not text:raise ValueError('MN missing action date/text')
            previous=when;spans=notes.find_all('span',recursive=False);journal='';note=[]
            for span in spans:
                value=span.get_text(' ',strip=True)
                if value.startswith('pg.'):journal=value[3:].strip()
                elif value:note.append(value)
            histories.append([record['number'],session['short'],chamber,when,text,journal,'; '.join(note),order,term,date_source])
    if not histories:raise ValueError('MN empty history')
    return detail,histories

def parse_senate_resolution_text(soup, record):
    """Parse Senate-hosted resolution text across official page templates."""
    number = record['number']

    match = re.fullmatch(r'(SR|SC)(\d{4})', number)

    if not match:
        raise ValueError(
            f'MN unexpected alternate Senate instrument: {number}'
        )

    prefix, digits = match.groups()
    expected = int(digits)

    identity_prefix = {
        'SR': r'S\.R\.',
        'SC': r'S\.C\.R\.',
    }[prefix]

    introduction = {
        'SR': 'A Senate resolution',
        'SC': 'A Senate concurrent resolution',
    }[prefix]

    containers = soup.select('.xtend')

    if len(containers) != 1:
        raise ValueError(
            f'MN expected one Senate resolution text container: {number}'
        )

    container = containers[0]

    # Both official Senate templates contain a resolution identity
    # and legislative-session heading.
    # The official Revisor status URL identifies the legislative session.
    source_path = urlparse(record['url']).path
    source_identity = re.fullmatch(
        r'/bills/94/202[56]/[01]/(SR|SC)/(\d+)/?',
        source_path
    )

    if (
        source_identity is None
        or source_identity.group(1) != prefix
        or int(source_identity.group(2)) != expected
    ):
        raise ValueError(
            f'MN Senate resolution source identity mismatch: {number}'
        )

    if re.fullmatch(
        r'/bills/94/2025/1/(SR|SC)/\d+/?',
        source_path
    ):
        session_pattern = r'2025 1st Special Session'
    elif re.fullmatch(
        r'/bills/94/202[56]/0/(SR|SC)/\d+/?',
        source_path
    ):
        session_pattern = (
            r'94th Legislative Session\s*\(2025-2026\)'
        )
    else:
        raise ValueError(
            f'MN unexpected Senate resolution source URL: {record["url"]}'
        )

    identity_pattern = (
        rf'\b{identity_prefix}\s*No\.\s*0*{expected}\s*,\s*'
        rf'as introduced:\s*{session_pattern}'
    )
    if not re.search(
        identity_pattern,
        soup.get_text(' ', strip=True),
        re.I
    ):
        raise ValueError(
            f'MN Senate resolution identity/session mismatch: {number}'
        )

    # Work on a copy so the original soup is unchanged.
    text = BeautifulSoup(str(container), 'lxml')

    for tag in text.select("var, a[id^='pl.']"):
        tag.decompose()

    body = re.sub(
        r'\s+', ' ',
        text.get_text(' ', strip=True)
    ).strip()

    match = re.match(
        rf'^({re.escape(introduction)}\b.+?\.)'
        r'(?=\s|$)',
        body,
        re.I
    )

    if match is None:
        raise ValueError(
            f'MN missing Senate resolution introductory text: {number}'
        )

    return match.group(1)

def parse_text(html, record):
    soup = BeautifulSoup(html, 'lxml')
    if (
        re.fullmatch(r'(SR|SC)\d{4}', record['number'])
        and soup.select_one('.xtend')
    ):
        return parse_senate_resolution_text(soup, record)
    heading = soup.select_one('h1.card-title')
    if (
        heading is None
        or re.sub(r'\s+', '', heading.get_text())
        != re.sub(
            r'([A-Z]+)0+(\d)',
            r'\1\2',
            record['number']
        )
    ):
        raise ValueError('MN wrong text bill identity')
    prolog = soup.select_one('.btitle_prolog')
    if prolog is None:
        raise ValueError('MN missing bill/resolution text prolog')
    paragraph = prolog.find_parent('p')
    if paragraph is None:
        raise ValueError('MN missing title paragraph')
    for line in paragraph.select('.pl'):
        line.decompose()
    return paragraph.get_text(' ', strip=True)

def _fetch(http,url):
    r=http.get(url,timeout=90);r.raise_for_status();time.sleep(1);return r.text


@scrape_run
def scrape(state,term,verbose=False,force_fetch=False):
    if state.upper()!='MN':raise ValueError('Minnesota scraper requires state MN')
    parse_term(term);folder=REPO_ROOT/'.data/MN/bill';cache=folder/'.cache'/term
    outputs=[folder/f'MN_{name}_{term}.csv' for name in ('Bill_Details','Bill_Histories')];manifest=folder/f'.MN_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping MN {term}: completed outputs exist (use --force-fetch to refresh)');return
    cache.mkdir(parents=True,exist_ok=True);details,histories,counts=[],[],{}
    with make_session() as http:
        session_cache = cache / 'session_search.html'

        if cache_io.exists(session_cache) and not force_fetch:
            session_html = cache_io.read_text(
                session_cache, encoding='utf-8'
            )
        else:
            session_html = _fetch(http, SEARCH_URL)
            select_sessions(session_html, term)
            cache_io.write_text(
                session_cache, session_html, encoding='utf-8'
            )

        sessions = select_sessions(session_html, term)
        for session in sessions:
            records=[];seen=set()
            for chamber in ('House','Senate'):
                url=BASE_URL+'/bills/status_result.php?'+urlencode({'body':chamber,'search':'advanced','session':session['id'],'submit_advanced':'GO','search_type':'andor','keyword_type':'all','size':100})
                received=0;total=None;page=1
                while url:
                    local=cache/f'{session["id"]}_{chamber}_index_{page}.html'
                    html=cache_io.read_text(local, encoding='utf-8') if cache_io.exists(local) and not force_fetch else _fetch(http,url)
                    group,first,last,n,next_url=parse_listing(html,session,chamber)
                    if first!=received+1 or (total is not None and total!=n):raise ValueError('MN search count changed or pagination gap')
                    for record in group:
                        if record['number'] in seen:raise ValueError('MN duplicate originating-chamber bill')
                        seen.add(record['number'])
                    records.extend(group);received=last;total=n;cache_io.write_text(local, html,encoding='utf-8');url=next_url;page+=1
                    if page%10==1 or url is None:print(f'MN {session["short"]} {chamber} index: {last}/{n}',flush=True)
            counts[session['short']]=len(records);print(f'MN {session["short"]}: {len(records)} unique originating-chamber instruments',flush=True)
            for i,record in enumerate(records,1):
                local=cache/f'{session["id"]}_{record["number"]}.html'
                html=cache_io.read_text(local, encoding='utf-8') if cache_io.exists(local) and not force_fetch else _fetch(http,record['url'])
                d,h=parse_bill(html,record,session,term);cache_io.write_text(local, html,encoding='utf-8')
                if d[-1]:
                    local=cache/f'{session["id"]}_{record["number"]}_text.html'
                    text=cache_io.read_text(local, encoding='utf-8') if cache_io.exists(local) and not force_fetch else _fetch(http,d[-1])
                    d[7]=parse_text(text,record);cache_io.write_text(local, text,encoding='utf-8')
                details.append(d);histories.extend(h)
                if verbose or i%25==0 or i==len(records):print(f'MN {session["short"]} {i}/{len(records)} {record["number"]}: {len(h)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged:cache_io.replace(pending, target)
        write_manifest(manifest, {'term':term,'details':len(details),'histories':len(histories),'session_counts':counts})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'MN {term}: saved {len(details):,} instruments and {len(histories):,} histories')
