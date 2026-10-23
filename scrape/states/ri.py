"""Rhode Island's official annual bill-text indexes and history reports."""
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

BASE_URL='https://webserver.rilegislature.gov'
HISTORY_URL='https://status.rilegislature.gov/bill_history_report.aspx'
REPO_ROOT=Path(__file__).resolve().parents[2]
DETAILS_HEADER=['bill_id','session','bill_type','intro_date','ref_comm','sponsors','sponsors_full','title','bill_url','history_url','term']
HISTORY_HEADER=['bill_id','session','action_date','action','order','term']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}',term):raise ValueError('RI term must be YYYY_YYYY')
    start,end=map(int,term.split('_'))
    if start<2013 or start%2!=1 or end!=start+1 or end>date.today().year:raise ValueError('RI requires an odd-starting consecutive term from 2013 without future years')
    return start,end


def parse_listing(html,year,chamber,url):
    soup=BeautifulSoup(html,'lxml');body='House' if chamber=='H' else 'Senate'
    if soup.title is None or soup.title.get_text(' ',strip=True)!=f'{year} {body} Bill Text' or '</html>' not in html.lower():raise ValueError('RI index year/chamber mismatch or truncated response')
    rows=[cell.find_parent('tr') for cell in soup.select('td.bill_col3')]
    if not rows:raise ValueError('RI empty bill index')
    records=[];excluded=[];seen=set()
    for row in rows:
        cell=row.select_one('.bill_col3');a=cell.find('a',href=True) if cell else None
        if a is None:raise ValueError('RI index row lacks HTML bill text')
        filename=a['href'];match=re.fullmatch(chamber+r'(\d+)\.htm',filename,re.I)
        if not match:
            if re.fullmatch(chamber+r'\d+[A-Za-z]+\.htm',filename,re.I) or filename.lower().startswith('article-'):
                excluded.append(filename);continue
            raise ValueError('RI unknown bill-text filename: '+filename)
        number=chamber+match[1].zfill(4)
        if number in seen:raise ValueError('RI duplicate base bill in index')
        seen.add(number);records.append({'number':number,'year':year,'url':urljoin(url,filename),'history_url':HISTORY_URL+f'?year={year}&bills={int(match[1])}'})
    if not records:raise ValueError('RI index contains no base bills')
    return records,excluded

def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def parse_intro_date(raw_date):
    """Handle known spelling variations in RI source dates."""
    normalized = re.sub(
        r'^Maart(?=\s)',
        'March',
        raw_date.strip(),
        flags=re.IGNORECASE
    )
    return datetime.strptime(normalized, '%B %d, %Y').date().isoformat()


def parse_bill(html,actions,record,term):
    soup=BeautifulSoup(html,'lxml');year=record['year'];number=record['number'];prefix=number[0];digits=int(number[1:])
    header=soup.select_one('p.RI_HEADER') or soup.find('h1')
    if header is None or not re.fullmatch(str(year)+r'\s*--\s*'+prefix+r'\s*0*'+str(digits),_text(header)):raise ValueError('RI bill text year/identity mismatch')
    titles=soup.select('p.TITLE') or soup.select('p.RI_TITLE')
    converted_kind=None
    if not titles and header and header.name=='h1':
        converted_kind=next((p for p in header.find_all_next('p') if re.sub(r'\s+','',p.get_text()) in ('ANACT','HOUSERESOLUTION','SENATERESOLUTION','JOINTRESOLUTION')),None)
        if converted_kind:
            title_node=next((p for p in converted_kind.find_next_siblings('p') if _text(p)),None)
            if title_node:titles=[title_node]
    title=' '.join(dict.fromkeys(_text(n) for n in titles if _text(n)))
    if not title:raise ValueError('RI missing bill title')
    def field(label):
        node=soup.find(['b','u'],string=re.compile(re.escape(label)))
        if node is None:raise ValueError('RI missing '+label)
        return _text(node.find_parent('p')).split(label,1)[1].strip()
    sponsors_full=field('Introduced By:');intro=parse_intro_date(field('Date Introduced:'));committee=field('Referred To:')
    kind=soup.select_one('p.TITLE_TYPE') or converted_kind
    if kind is None:raise ValueError('RI missing instrument type')
    bill_type=re.sub(r'\s+','',kind.get_text())
    source=BeautifulSoup(actions,'lxml');form=source.select_one('form[action]');params=parse_qs(urlparse(form['action']).query) if form else {}
    if params.get('year')!=[str(year)] or params.get('bills')!=[str(digits)]:raise ValueError('RI history form identity mismatch')
    block=source.select_one('#lblBills')
    if block is not None and 'No Bills Met this Criteria' in _text(block) and re.search(r'Total Bills:\s*0\b',_text(block)):
        # The text archive contains some instruments absent from the status
        # database. Preserve their verified text metadata without inventing actions.
        return [number,year,bill_type,intro,committee,'',sponsors_full,title,record['url'],record['history_url'],term],[]
    if block is None or not re.search(r'Total Bills:\s*1\b',block.get_text(' ',strip=True)):raise ValueError('RI history missing/ambiguous bill')
    # The linked bill PDF identifies the source year and chamber independently.
    links=[a for a in block.select('a[href]') if re.search(r'/'+number+r'[A-Za-z]*\.pdf$',a['href'],re.I)]
    if len(links)!=1 or f'BillText{str(year)[2:]}/' not in links[0]['href']:raise ValueError('RI history linked bill identity mismatch')
    by=block.find('b',string=lambda t:t and t.strip()=='BY')
    if by is None:raise ValueError('RI history lacks sponsor field')
    sponsors=_text(by.parent).removeprefix('BY').strip()
    histories=[]
    for action in block.select('div[style]'):
        if not re.search(r'margin-left:\s*5%',action['style']):continue
        text=_text(action);m=re.fullmatch(r'(\d{2}/\d{2}/\d{4})\s+(.+)',text)
        if not m:raise ValueError('RI malformed dated history row')
        when=datetime.strptime(m[1],'%m/%d/%Y').date()
        if when.year!=year:raise ValueError('RI action year differs from annual session')
        histories.append([number,year,when.isoformat(),m[2],len(histories)+1,term])
    if not histories:raise ValueError('RI empty history')
    detail=[number,year,bill_type,intro,committee,sponsors,sponsors_full,title,record['url'],record['history_url'],term]
    return detail,histories


def _cached(http,path,url,force):
    if cache_io.exists(path) and not force:return cache_io.read_text(path)
    r=http.get(url,timeout=90);r.raise_for_status();time.sleep(1)
    pending=path.with_suffix('.html.tmp');cache_io.write_text(pending, r.text,encoding='utf-8');cache_io.replace(pending, path);return r.text


@scrape_run
def scrape(state,term,verbose=False,force_fetch=False):
    if state.upper()!='RI':raise ValueError('Rhode Island scraper requires state RI')
    years=parse_term(term);folder=REPO_ROOT/'.data/RI/bill';cache=folder/'.cache'/term;cache.mkdir(parents=True,exist_ok=True)
    outputs=[folder/f'RI_{kind}_{term}.csv' for kind in ('Bill_Details','Bill_Histories')];manifest=folder/f'.RI_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping RI {term}: completed outputs exist (use --force-fetch to refresh)');return
    details,histories,counts,excluded=[],[],{},{};empty_histories=[]
    with make_session() as http:
        for year in years:
            records=[];short=str(year)[2:]
            for chamber,body in (('H','House'),('S','Senate')):
                url=BASE_URL+f'/BillText{short}/{body}Text{short}/{body}Text{short}.html'
                rows,skip=parse_listing(_cached(http,cache/f'{year}_{chamber}_index.html',url,force_fetch),year,chamber,url);records.extend(rows);counts[str(year)+chamber]=len(rows);excluded[str(year)+chamber]=skip
            print(f'RI {year}: {len(records)} base bills/resolutions',flush=True)
            for i,record in enumerate(records,1):
                html=_cached(http,cache/f'{year}_{record["number"]}.html',record['url'],force_fetch);actions=_cached(http,cache/f'{year}_{record["number"]}_history.html',record['history_url'],force_fetch)
                d,h=parse_bill(html,actions,record,term);details.append(d);histories.extend(h)
                if not h:empty_histories.append(record)
                if verbose or i%25==0 or i==len(records):print(f'RI {year} {i}/{len(records)} {record["number"]}: {len(h)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged:cache_io.replace(pending, target)
        write_manifest(manifest, {'term':term,'counts':counts,'details':len(details),'histories':len(histories),'excluded_substitute_versions_and_articles':excluded,'source_empty_histories':empty_histories,})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'RI {term}: wrote {len(details)} details and {len(histories)} histories',flush=True)
