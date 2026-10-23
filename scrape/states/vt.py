"""Vermont's official bill indexes, sponsor pages and complete action feeds."""
from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from utils import cache as cache_io
import csv
from datetime import date, datetime
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scrape.http import make_session

BASE_URL='https://legislature.vermont.gov/'
REPO_ROOT=Path(__file__).resolve().parents[2]
DETAILS_HEADER=['bill_id','session','act_num','version_status','primary_sponsor','cosponsors','title','statutes','bill_url','term','source_id']
HISTORY_HEADER=['bill_id','session','chamber','action_date','status','action','order','journal_page','calendar_page','document_url','vote_url','term']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}',term):raise ValueError('VT term must be YYYY_YYYY')
    start,end=map(int,term.split('_'))
    if start<2009 or start%2!=1 or end!=start+1 or end>date.today().year:raise ValueError('VT requires an odd-starting consecutive term from 2009 without future years')
    return start,end


def _text(node):return ' '.join(node.get_text(' ',strip=True).split()) if node else ''


def select_sessions(html,term):
    years=parse_term(term);soup=BeautifulSoup(html,'lxml');sessions=[]
    for option in soup.select('#Form_SelectSession_selected_session option'):
        label=_text(option);sid=option['value']
        if label==term.replace('_','-')+' Session':sessions.append({'id':sid,'label':term+'_RS'})
        elif re.fullmatch(r'\d{4} Special Session',label) and int(label[:4]) in years:sessions.append({'id':sid,'label':label[:4]+'_SS_'+sid})
    sessions=list({s['id']:s for s in sessions}.values())
    if sum(s['label']==term+'_RS' for s in sessions)!=1:raise ValueError('VT requested regular session not cataloged')
    return sessions


def parse_listing(data,session,term):
    rows=data.get('data')
    if not isinstance(rows,list) or not rows:raise ValueError('VT missing bill/resolution index')
    years=set(map(str,parse_term(term)));seen=set()
    for row in rows:
        number=row['BillNumber']
        if not re.fullmatch(r'(?:[HS](?:\.[A-Z])*|J\.R\.[HS]|PR)\.\d+',number) or row['year'] not in years:raise ValueError('VT index number/year mismatch')
        if number in seen:raise ValueError('VT duplicate index number')
        seen.add(number)
        if not row['Title'].strip():raise ValueError('VT index has no title')
    return rows


def parse_detail(html,record,session,term):
    soup=BeautifulSoup(html,'lxml');heading=soup.select_one('.bill-title h1');number=record['BillNumber'];act=heading.find('span') if heading else None
    visible=' '.join(str(s) for s in heading.find_all(string=True,recursive=False)).strip() if heading else ''
    if visible!=number:raise ValueError('VT detail bill identity mismatch')
    selected=soup.select_one('#Form_SelectSession_selected_session option[selected]')
    if selected is None or selected['value']!=session['id']:raise ValueError('VT detail session mismatch')
    title = _text(soup.select_one('.bill-title h2'))
    index_title = ' '.join(record['Title'].split())
    if not title:
        raise ValueError('VT detail page missing title')
    if title != index_title:
        duplicated_suffix = (
            'a similar intervention to disrupt or destroy the right '
            'to self-determination and political sovereignty of any other nation'
        )
        known_source_error = (
            session['id'] == '2026'
            and number == 'J.R.H.8'
            and index_title == title + duplicated_suffix
        )
        if not known_source_error:
            raise ValueError('VT detail/index title mismatch')
    sponsors = soup.select_one('#bill-sponsors-list')

    known_omitted_resolution = (
        session['id'] == '2026'
        and number == 'H.C.R.163'
        and title == 'Omitted'
    )

    if sponsors is None and not number.startswith('PR.') and not known_omitted_resolution:
        raise ValueError('VT missing sponsor list')

    primary=[];cosponsors=[]
    for li in (sponsors.find_all('li',recursive=False) if sponsors else []):
        name=_text(li)
        if not name or name in ('Additional Sponsors','Less…'):continue
        (cosponsors if 'sponsor' in li.get('class',[]) else primary).append(name)
    if not primary and not number.startswith('PR.') and not known_omitted_resolution:
        raise ValueError('VT missing primary sponsor')
    number_clean=number.replace('.','');m=re.fullmatch(r'([A-Z]+)(\d+)',number_clean);bill=m[1]+m[2].zfill(4)
    paths=set(re.findall(r'bill/loadBillDetailedStatus/([\d.]+)/([\d]+)',html))
    if len(paths)!=1:raise ValueError('VT missing/ambiguous history endpoint')
    sid,source_id=paths.pop()
    if sid!=session['id']:raise ValueError('VT action endpoint session mismatch')
    versions='; '.join(_text(a) for a in soup.select('ul.bill-path a'))
    statutes='; '.join(_text(td) for td in soup.select('#bill-related-statutes td'))
    detail=[bill,session['label'],_text(act).strip(' ()') or record.get('ActNo',''),versions,'; '.join(primary),'; '.join(cosponsors),title,statutes,BASE_URL+f'bill/status/{sid}/{number}',term,source_id]
    return detail,BASE_URL+f'bill/loadBillDetailedStatus/{sid}/{source_id}'


def parse_actions(data,detail,session,term):
    rows=data.get('data')
    if not isinstance(rows,list) or not rows:raise ValueError('VT missing history rows')
    histories=[];seen=set()
    for action in rows:
        if str(action['Biennium'])!=session['id'] or action['Sequence'] in seen:raise ValueError('VT history session mismatch/duplicate sequence')
        seen.add(action['Sequence']);when=datetime.strptime(action['StatusDate'],'%m/%d/%Y').date().isoformat();text=_text(BeautifulSoup(action['FullStatus'],'lxml'))
        if not text:raise ValueError('VT blank action')
        histories.append([detail[0],session['label'],action['Chamber'],when,action['Location'],text,int(action['Sequence'])+1,action['JournalPage'],action['CalendarPage'],urljoin(BASE_URL,action['Url']) if action['Url'] else '',BASE_URL+f'bill/roll-call/{session["id"]}/{action["VoteHeaderID"]}' if action['VoteHeaderID'] else '',term])
    return histories


def _cached(http,path,url,force):
    if cache_io.exists(path) and not force:return cache_io.read_text(path)
    r=http.get(url,timeout=90);r.raise_for_status();r.encoding='utf-8';time.sleep(0.75)
    pending=path.with_suffix(path.suffix+'.tmp');cache_io.write_text(pending, r.text);cache_io.replace(pending, path);return r.text


@scrape_run
def scrape(state,term,verbose=False,force_fetch=False):
    if state.upper()!='VT':raise ValueError('Vermont scraper requires state VT')
    years=parse_term(term);folder=REPO_ROOT/'.data/VT/bill';cache=folder/'.cache'/term;cache.mkdir(parents=True,exist_ok=True)
    outputs=[folder/f'VT_{kind}_{term}.csv' for kind in ('Bill_Details','Bill_Histories')];manifest=folder/f'.VT_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping VT {term}: completed outputs exist (use --force-fetch to refresh)');return
    details,histories,counts=[],[],{}
    title_mismatches = []
    with make_session() as http:
        sessions=select_sessions(_cached(http,cache/'sessions.html',BASE_URL+f'bill/search/{years[1]}',force_fetch),term)
        for session in sessions:
            sid=session['id'];records=[]
            for endpoint in ('loadBillsIntroduced','loadAllResolutionsByChamber'):
                data=json.loads(_cached(http,cache/(sid+'_'+endpoint+'.json'),BASE_URL+f'bill/{endpoint}/{sid}',force_fetch));records.extend(parse_listing(data,session,term))
            if len({r['BillNumber'] for r in records})!=len(records):raise ValueError('VT overlapping bill indexes')
            counts[sid]=len(records);print(f'VT {sid}: {len(records)} instruments',flush=True)
            for i,record in enumerate(records,1):
                number = record['BillNumber']
                html = _cached(
                    http,
                    cache / (sid + '_' + number + '.html'),
                    BASE_URL + f'bill/status/{sid}/{number}',
                    force_fetch
                )

                d, url = parse_detail(html, record, session, term)

                index_title = ' '.join(record['Title'].split())
                detail_title = d[6]

                if index_title != detail_title:
                    title_mismatches.append({
                        'bill': number,
                        'session': session['label'],
                        'index_title': index_title,
                        'detail_title': detail_title,
                        'url': d[8],
                    })
                actions=json.loads(_cached(http,cache/(sid+'_'+number+'_actions.json'),url,force_fetch));h=parse_actions(actions,d,session,term);details.append(d);histories.extend(h)
                if verbose or i%25==0 or i==len(records):print(f'VT {sid} {i}/{len(records)} {number}: {len(h)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged:cache_io.replace(pending, target)
        write_manifest(manifest, {'term':term,'sessions':sessions,'counts':counts,'details':len(details),'histories':len(histories),'source_title_mismatches': title_mismatches,'source_without_primary_sponsor':[{'bill':d[0],'session':d[1],'url':d[8]} for d in details if not d[4]],})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'VT {term}: wrote {len(details)} details and {len(histories)} histories',flush=True)
