"""Washington public legislation services and complete bill-summary histories."""
from __future__ import annotations

from scrape.reporting import scrape_run, write_manifest

from utils import cache as cache_io
import csv
from datetime import date, datetime
from pathlib import Path
import re
import time
from urllib.parse import urlencode,urljoin
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from scrape.http import make_session

API='https://wslwebservices.leg.wa.gov/legislationservice.asmx/'
BASE_URL='https://app.leg.wa.gov/billsummary'
REPO_ROOT=Path(__file__).resolve().parents[2]
DETAILS_HEADER=['bill_id','session','biennium','bill_type','start_chamber','primary_sponsors','cosponsors','title','summary','intro_date','appropriations','veto','partial_veto','companion','bill_url','term']
HISTORY_HEADER=['bill_id','session','session_year','session_type','chamber','action_date','action','order','links','date_source','term']


def parse_term(term):
    if not re.fullmatch(r'\d{4}_\d{4}',term):raise ValueError('WA term must be YYYY_YYYY')
    start,end=map(int,term.split('_'))
    if start<1991 or start%2!=1 or end!=start+1 or end>date.today().year:raise ValueError('WA requires an odd-starting consecutive term from 1991 without future years')
    return start,end


def xml_root(text,expected):
    root=ET.fromstring(text)
    for e in root.iter():e.tag=e.tag.split('}')[-1]
    if root.tag!=expected:raise ValueError('WA unexpected XML root: '+root.tag)
    return root


def parse_listing(text,term):
    start,end=parse_term(term);biennium=f'{start}-{str(end)[2:]}';root=xml_root(text,'ArrayOfLegislationInfo');records={}
    for row in root:
        if row.findtext('Biennium')!=biennium:raise ValueError('WA index biennium mismatch')
        if row.findtext('SubstituteVersion')!='0' or row.findtext('EngrossedVersion')!='0':continue
        number=row.findtext('BillId');digits=row.findtext('BillNumber');kind=row.findtext('ShortLegislationType/LongLegislationType')
        valid=bool(number and digits and number.endswith(' '+digits))
        if kind=='Initiative' and number:
            initiative=re.fullmatch(r'[HS]I IL(\d{2})-(\d{3})',number)
            valid=valid or bool(initiative and str(int(initiative[1]+initiative[2]))==digits)
        if not valid:raise ValueError('WA invalid bill identity: '+str(number))
        record={'number':number,'digits':digits,'biennium':biennium,'kind':kind,'chamber':row.findtext('OriginalAgency')}
        if number in records and record!=records[number]:raise ValueError('WA conflicting indexed bill')
        records[number]=record
    if not records:raise ValueError('WA empty annual index')
    return records


def _text(node):return ' '.join(node.get_text(' ',strip=True).split()) if node else ''


def parse_bill(xml,sponsors,html,record,term):
    start,end=parse_term(term);root=xml_root(xml,'ArrayOfLegislation');number=record['number'];biennium=record['biennium']
    rows=[r for r in root if r.findtext('BillId')==number and r.findtext('SubstituteVersion')=='0' and r.findtext('EngrossedVersion')=='0']
    if len(rows)>1:
        dated=[r for r in rows if (r.findtext('CurrentStatus/ActionDate') or '').startswith(('19','20'))]
        # The API also emits undated placeholder copies of some base bills.
        # Require one real status record and identical descriptive metadata.
        fields=('Biennium','BillNumber','LegalTitle','LongDescription','IntroducedDate')
        if len(dated)==1 and all(all(r.findtext(k)==dated[0].findtext(k) for k in fields) for r in rows):rows=dated
    if len(rows)!=1:raise ValueError('WA base bill missing/ambiguous in details')
    row=rows[0]
    if row.findtext('Biennium')!=biennium or row.findtext('BillNumber')!=record['digits']:raise ValueError('WA detail identity mismatch')
    sponsors=xml_root(sponsors,'ArrayOfSponsor');primary=[];secondary=[]
    for sponsor in sponsors:
        role=sponsor.findtext('Type');name=', '.join(filter(None,[sponsor.findtext('LastName'),sponsor.findtext('FirstName')])) or sponsor.findtext('Name')
        if role not in ('Primary','Secondary') or not name:raise ValueError('WA unknown sponsor role or missing name')
        (primary if role=='Primary' else secondary).append(name)
    # Gubernatorial appointments and initiatives do not require
    # legislative primary sponsors.
    sponsor_exempt = (
        record['kind'] == 'Initiative'
        or number.startswith('SGA ')
    )
    if not primary and not sponsor_exempt:
        raise ValueError(
            f'WA missing primary sponsor: {number}'
        )
    def value(path):return row.findtext(path) or ''
    url=BASE_URL+'?'+urlencode({'BillNumber':record['digits'],'Initiative':str(record['kind']=='Initiative').lower(),'Year':start})
    detail=[number.replace(' ',''),term,biennium,record['kind'],record['chamber'],'; '.join(primary),'; '.join(secondary),value('LegalTitle'),value('LongDescription'),value('IntroducedDate').split('T')[0],value('Appropriations'),value('CurrentStatus/Veto'),value('CurrentStatus/PartialVeto'),'; '.join(c.findtext('BillId') or '' for c in row.findall('Companions/Companion')),url,term]
    if not detail[7] and not detail[8]:
        if not number.startswith('SGA '):
            raise ValueError(
                f'WA missing title/description: {number}'
            )
    soup=BeautifulSoup(html,'lxml')
    if record['kind'] == 'Initiative':
        match = re.fullmatch(
            r'[HS]I (IL\d{2}-\d{3})',
            number
        )
        if not match:
            raise ValueError(
                f'WA malformed initiative identity: {number}'
            )
        expected_title = (
            f'Initiative {match.group(1)} Washington State Legislature'
        )
    else:
        expected_title = number + ' Washington State Legislature'

    if _text(soup.title) != expected_title:
        raise ValueError(
            f'WA history page bill mismatch: {number}'
        )
    selected=soup.select_one('#Year option[selected]')
    if selected is None or selected.get('value')!=str(start):raise ValueError('WA history page biennium mismatch')
    tables=soup.select('.historytable')
    if not tables:raise ValueError('WA missing history sections')
    histories=[]
    for table in tables:
        header=table.find_previous('h3');chamber_header=table.find_previous('h4');label=_text(header);m=re.fullmatch(r'(\d{4}) (Regular Session|\d+(?:st|nd|rd|th) Special Session)',label,re.I)
        if not m or int(m[1]) not in (start,end):raise ValueError('WA unknown history session: '+label)
        year=int(m[1]);chamber=_text(chamber_header).removeprefix('In the ')
        if chamber not in ('House','Senate','Governor','Other than legislative action'):raise ValueError('WA unknown history chamber: '+chamber)
        last_date=None
        for item in table.find_all('div',recursive=False):
            cells=item.find_all('div',recursive=False)
            if len(cells)!=2:raise ValueError('WA malformed history row')
            raw=_text(cells[0]);node=BeautifulSoup(str(cells[1]),'lxml');links='; '.join(urljoin(BASE_URL,a['href']) for a in node.select('a[href]'))
            for a in node.select('a'):a.decompose()
            action=_text(node)
            if not action:raise ValueError('WA empty action')
            source='section year and printed month/day'
            if raw:
                parsed=datetime.strptime(raw+' '+str(year),'%b %d %Y');action_year=year
                if 'prefiled' in action.lower() and parsed.month>=11:action_year=year-1;source='prefiled in November/December before session year'
                last_date=date(action_year,parsed.month,parsed.day).isoformat()
            else:source='same printed date as previous row'
            if last_date is None:raise ValueError('WA undated first action')
            histories.append([detail[0],term,year,m[2],chamber,last_date,action,len(histories)+1,links,source,term])
    if not histories:raise ValueError('WA empty history')
    return detail,histories


def _cached(http,path,url,force):
    if cache_io.exists(path) and not force:return cache_io.read_text(path)
    r=http.get(url,timeout=90);r.raise_for_status();time.sleep(0.75)
    pending=path.with_suffix(path.suffix+'.tmp');cache_io.write_text(pending, r.text);cache_io.replace(pending, path);return r.text


@scrape_run
def scrape(state,term,verbose=False,force_fetch=False):
    if state.upper()!='WA':raise ValueError('Washington scraper requires state WA')
    years=parse_term(term);folder=REPO_ROOT/'.data/WA/bill';cache=folder/'.cache'/term;cache.mkdir(parents=True,exist_ok=True)
    outputs=[folder/f'WA_{kind}_{term}.csv' for kind in ('Bill_Details','Bill_Histories')];manifest=folder/f'.WA_scrape_{term}.json'
    if all(cache_io.exists(p) for p in [*outputs,manifest]) and not force_fetch:
        print(f'Skipping WA {term}: completed outputs exist (use --force-fetch to refresh)');return
    details,histories,counts=[],[],{}
    with make_session() as http:
        http.headers['User-Agent']='Mozilla/5.0';records={}
        for year in years:
            rows=parse_listing(_cached(http,cache/f'{year}_index.xml',API+f'GetLegislationByYear?year={year}',force_fetch),term);counts[year]=len(rows)
            for number,record in rows.items():
                if number in records and record!=records[number]:raise ValueError('WA annual index conflict')
                records[number]=record
        print(f'WA {term}: {len(records)} unique base instruments',flush=True)
        for i,record in enumerate(records.values(),1):
            number=record['number'];base=number.replace(' ','');params={'biennium':record['biennium'],'billNumber':record['digits']}
            xml=_cached(http,cache/(base+'.xml'),API+'GetLegislation?'+urlencode(params),force_fetch)
            if record['kind'] == 'Initiative':
                # Initiatives are sponsored by the people, not legislators.
                # The sponsor service does not accept initiative bill IDs.
                sponsors = '<ArrayOfSponsor></ArrayOfSponsor>'
            else:
                sponsors = _cached(
                    http,
                    cache / (base + '_sponsors.xml'),
                    API + 'GetSponsors?' + urlencode({
                        'biennium': record['biennium'],
                        'billId': number,
                    }),
                    force_fetch,
                )
            url=BASE_URL+'?'+urlencode({'BillNumber':record['digits'],'Initiative':str(record['kind']=='Initiative').lower(),'Year':years[0]})
            html=_cached(http,cache/(base+'.html'),url,force_fetch);d,h=parse_bill(xml,sponsors,html,record,term);details.append(d);histories.extend(h)
            if verbose or i%25==0 or i==len(records):print(f'WA {i}/{len(records)} {number}: {len(h)} actions',flush=True)
    staged=[]
    try:
        for target,header,rows in zip(outputs,(DETAILS_HEADER,HISTORY_HEADER),(details,histories)):
            pending=target.with_suffix('.csv.tmp');staged.append((pending,target))
            with pending.open('w',newline='',encoding='utf-8') as handle:
                writer=csv.writer(handle);writer.writerow(header);writer.writerows(rows)
        cache_io.unlink(manifest, missing_ok=True)
        for pending,target in staged:cache_io.replace(pending, target)
        write_manifest(manifest, {'term':term,'annual_base_counts':counts,'details':len(details),'histories':len(histories),})
    finally:
        for pending,_ in staged:cache_io.unlink(pending, missing_ok=True)
    print(f'WA {term}: wrote {len(details)} details and {len(histories)} histories',flush=True)
