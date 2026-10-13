"""Bounded public industry facts and attributed events; no forecast or trading."""
import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from . import sources as base

CATALOG = {
    'industry_hibor': ('inventory,mto', 'republication', 'huatai_report',
        'https://m.hibor.com.cn/wap_detail.aspx?id=f8ff93dd66cb17c88e89540b7a75b2d4'),
    'industry_rdqh': ('inventory,mto', 'broker_report', 'rdqh_upstream_unverified',
        'https://www.rdqh.com/upload/files/base/2/n/107664.pdf'),
    'industry_oilchem': ('inventory', 'original_provider', 'oilchem',
        'https://www.oilchem.net/26-1008-13-8de4a03193daaea7.html'),
    'industry_mfa': ('diplomacy', 'official_statement', 'mfa',
        'https://www.mfa.gov.cn/web/wjbz_673089/xghd_673097/202607/t20260725_11991990.shtml'),
    'industry_portwatch': ('hormuz', 'official_ais_estimate', 'imf_portwatch',
        'https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query'),
    'industry_ukmto': ('hormuz', 'official_attributed_report', 'ukmto',
        'https://sccd.royalnavy.mod.uk/api/ukmto/all'),
    'industry_auction_lead': ('auction', 'republication_lead', 'unverified_seller',
        'https://xinjiang.jianyu360.cn/jybx/20260922_26092188012182.html'),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS industry_event (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL REFERENCES source(id), category TEXT NOT NULL,
 event_key TEXT NOT NULL, event_type TEXT NOT NULL, stage TEXT NOT NULL, actor TEXT,
 event_start TEXT, event_end TEXT, event_precision TEXT NOT NULL, published_at TEXT,
 publication_text TEXT, first_seen TEXT NOT NULL, processed_at TEXT NOT NULL,
 available_at TEXT NOT NULL, availability_basis TEXT NOT NULL,
 confidence TEXT NOT NULL, quality TEXT NOT NULL, detail TEXT NOT NULL,
 raw_hash TEXT NOT NULL, attempt_id INTEGER NOT NULL REFERENCES ingest_attempt(id),
 fingerprint TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS industry_event_evidence (
 event_id INTEGER NOT NULL REFERENCES industry_event(id),
 attempt_id INTEGER NOT NULL REFERENCES ingest_attempt(id),PRIMARY KEY(event_id,attempt_id));
CREATE TABLE IF NOT EXISTS auction_fact (
 event_id INTEGER PRIMARY KEY REFERENCES industry_event(id),project_id TEXT,
 direction TEXT NOT NULL, product TEXT,location TEXT,specification TEXT,auction_time_text TEXT,
 starting_price REAL,clearing_price REAL,planned_quantity REAL,actual_quantity REAL,unsold_quantity REAL,
 price_unit TEXT,quantity_unit TEXT,delivery_terms TEXT,result_status TEXT NOT NULL);
'''


def connect(path):
    # Register supported, grounded endpoints; the existing schema/migrations remain authoritative.
    for name, (_, confidence, lineage, url) in CATALOG.items():
        base.SOURCES[name] = (confidence, lineage, url)
    db = base.connect(path)
    db.executescript(SCHEMA)
    return db


def event(category, key, kind, stage, actor=None, start=None, end=None,
          precision='unknown', publication_text=None, confidence='unverified', flags=(), detail=None):
    return dict(category=category,event_key=key,event_type=kind,stage=stage,actor=actor,
                event_start=start,event_end=end,event_precision=precision,published_at=None,
                publication_text=publication_text,confidence=confidence,
                quality=','.join(sorted(set(flags))) or 'attributed_record',detail=detail or {})


def parse_report(raw):
    """Extract public Huatai abstract values, preserving unknown per-field periods."""
    lines=base.page_lines(raw)
    start=next((i for i,x in enumerate(lines) if x=='市场要闻与重要数据'),None)
    if start is None: raise ValueError('report data section absent')
    stop=next((i for i in range(start+1,len(lines)) if lines[i]=='市场分析'),len(lines))
    section='\n'.join(lines[start+1:stop])
    publication=next((x for x in lines[:start] if re.match(r'20\d{2}-\d{2}-\d{2} \d{2}:\d{2}',x)),None)
    rows=[]
    specs=[('mto_external_operating_rate',r'外采甲醇MTO企业开工率([\d.]+)%','%', '中国'),
           ('mto_all_operating_rate',r'(?<!甲醇)MTO企业开工率([\d.]+)%','%', '中国'),
           ('port_inventory',r'甲醇港口库存吨([\d,]+)','吨','总计')]
    for region in ('江苏','浙江','广东','福建'):
        specs.append(('port_inventory',region+r'([\d,]+)吨','吨',region))
    for metric,pattern,unit,region in specs:
        matches=list(re.finditer(pattern,section))
        if len(matches)!=1: raise ValueError('missing/ambiguous report field: '+metric+'/'+region)
        m=matches[0]; value=base.number(m[1])
        if unit=='%' and value>100: raise ValueError('invalid rate')
        line=next(x for x in lines[start+1:stop] if m[0] in x)
        rows.append(base.record(metric,value,unit,region=region,detail={
            'excerpt':line,'publication_text':publication,'publication_timezone':None,
            'report_provider':'华泰期货','underlying_data_provider':'not_explicit_in_public_abstract',
            'coverage':'外采甲醇MTO' if metric.startswith('mto_external') else '全口径MTO' if metric.startswith('mto_all') else '港口库存',
            'period_status':'unknown_do_not_use_article_date'},flags=['underlying_provider_unverified']))
    total=next(r['value'] for r in rows if r['region']=='总计')
    parts=sum(r['value'] for r in rows if r['metric']=='port_inventory' and r['region']!='总计')
    if total!=parts:
        for r in rows:
            if r['metric']=='port_inventory':r['quality']+=',regional_sum_mismatch'
    return rows


def parse_mfa(raw):
    lines=base.page_lines(raw)
    title=next((x for x in lines if x.startswith('王毅会见伊朗外长')),None)
    intro=next((x for x in lines if re.match(r'20\d{2}年\d+月\d+日，.*会见伊朗外长',x)),None)
    if not title or not intro: raise ValueError('MFA meeting layout changed')
    m=re.match(r'(20\d{2})年(\d+)月(\d+)日',intro)
    day=date(*map(int,m.groups())).isoformat()
    publication=next((x for x in lines if re.fullmatch(r'20\d{2}-\d{2}-\d{2} \d{2}:\d{2}',x)),None)
    rows=[]
    for prefix,actor,stage in [('王毅表示','中国外交部长王毅','advocacy'),('阿拉格齐通报','伊朗外长阿拉格齐','stated_willingness')]:
        matches=[x for x in lines if x.startswith(prefix)]
        if len(matches)!=1 or '谈判' not in matches[0]: raise ValueError('MFA attributed statement missing')
        rows.append(event('diplomacy',day+'/'+actor,'diplomatic_statement',stage,actor,day,day,'day',publication,
            'official_record_of_attributed_statement',['publication_timezone_unknown','not_independent_confirmation_of_agreement'],
            {'excerpt':matches[0],'meeting_intro':intro,'publisher':'中华人民共和国外交部',
             'assertion_scope':'speaker_statement_only','announced':None,'talks_started':None,
             'agreement_reached':None,'agreement_implemented':None,'not_current_news':True}))
    return rows


def parse_portwatch(raw):
    doc=json.loads(base.decode(raw))
    if 'error' in doc or not doc.get('features'):raise ValueError('PortWatch query error/empty')
    rows=[];seen=set()
    for f in doc['features']:
        a=f['attributes']; day=date.fromisoformat(a['date']).isoformat()
        if a['portid']!='chokepoint6' or a['portname']!='Strait of Hormuz' or day in seen:
            raise ValueError('wrong/duplicate chokepoint/date')
        seen.add(day)
        total,tanker=base.number(a['n_total']),base.number(a['n_tanker'])
        if total is None or tanker is None or not total.is_integer() or not tanker.is_integer() or tanker>total:
            raise ValueError('invalid AIS vessel counts')
        for metric,value,unit in [('ais_transits_total',total,'vessel_transits'),
                                  ('ais_transits_tanker',tanker,'vessel_transits'),
                                  ('ais_reported_capacity',base.number(a.get('capacity')),'source_capacity_unit_unverified')]:
            rows.append(base.record(metric,value,unit,day,day,region='Strait of Hormuz',detail={
                'excerpt':a,'portid':a['portid'],'estimate_basis':'AIS-derived',
                'coverage':'bounded_latest_7_rows_not_complete_history',
                'response_exceeded_transfer_limit':doc.get('exceededTransferLimit',False),
                'limitations':['AIS_nonbroadcast_or_spoofing_can_cause_missing_or_biased_counts',
                               'vessel_counts_and_capacity_are_not_methanol_tonnage'],
                'publication_time':None},flags=['ais_estimate','capacity_unit_unverified'] if 'capacity' in metric else ['ais_estimate']))
    return rows


def parse_ukmto(raw):
    data=json.loads(base.decode(raw))
    if not isinstance(data,list):raise ValueError('UKMTO API layout changed')
    selected=[x for x in data if x.get('incidentIssuer')=='UKMTO' and x.get('place')=='Strait of Hormuz']
    if not selected:raise ValueError('no Hormuz records')
    events=[]
    # Bound the sample; these are warning/incident records, not unique ships or totals.
    for x in sorted(selected,key=lambda x:x['utcDateCreated'])[-10:]:
        text=x['otherDetails'];api_time=x.get('utcDateOfIncident');created=x.get('utcDateCreated')
        if api_time:base.timestamp(api_time)
        if created:base.timestamp(created)
        flags=['official_report_not_independent_incident_confirmation','api_date_role_unverified']
        late='time-late' in text.lower()
        # Prefer explicit Incident Date/Time in the warning. Do not use ingestion-like API timestamps.
        m=re.search(r'Incident Date:\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})\s*Incident Time:\s*(\d{4})\s*UTC',text,re.I)
        when=None; precision='unknown'
        if m:
            year=int(m[3]);year=2000+year if year<100 else year
            when=datetime.strptime(f'{m[1]} {m[2]} {year} {m[4]}','%d %b %Y %H%M').replace(tzinfo=timezone.utc).isoformat()
            precision='minute'
            if api_time and base.timestamp(api_time)!=base.timestamp(when):flags.append('api_and_text_event_time_disagree')
        else:
            dated=re.search(r'Incident Date:\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2,4})',text,re.I)
            if dated:
                year=int(dated[3]);year=2000+year if year<100 else year
                when=datetime.strptime(f'{dated[1]} {dated[2]} {year}','%d %b %Y').date().isoformat()
                precision='day';flags.append('unknown_actual_event_clock_time')
        if not when:flags.append('unknown_actual_event_time')
        if late:flags.append('delayed_report')
        events.append(event('hormuz',x['sitecoreId'],'maritime_security_report',x['incidentTypeName'],
            'UKMTO',when,when,precision,None,'official_attributed_report',flags,
            {'excerpt':text,'incident_number':x['incidentNumber'],'api_created_at':created,
             'api_incident_at':api_time,'api_created_is_not_verified_publication':True,
             'place':x['place'],'actual_delay_seconds':None,'delayed_report':late,
             'source_page':'https://www.ukmto.org/recent-incidents#'+x['sitecoreId'],
             'bounded_sample':'latest_10_Hormuz_records','ship_type':x.get('vesselType'),
             'not_methanol_volume':True}))
    return events


def parse_auction(raw):
    text='\n'.join(base.page_lines(raw))
    if '甲醇' in text and '采购流标公告' in text and '销售产品竞卖公告' not in text:
        auction=dict(project_id=None,direction='buyer_procurement',product='甲醇',location=None,
            specification=None,auction_time_text=None,starting_price=None,clearing_price=None,
            planned_quantity=None,actual_quantity=None,unsold_quantity=None,price_unit='CNY/t',
            quantity_unit='t',delivery_terms=None,result_status='buyer_procurement_failed')
        return [event('auction','buyer_failed/'+base.digest(raw),'buyer_procurement_failure','reported_failed',
            confidence='unverified_republication_lead',flags=['original_buyer_notice_unverified','not_seller_unsold'],
            detail={'auction':auction,'excerpt':text[:1800],'does_not_establish_unsold_or_mto_demand':True})]
    # Ignore aggregator category labels, which can incorrectly label a planned sale as a result.
    if '销售产品竞卖公告' not in text:raise ValueError('no supported seller notice body')
    text=text.split('销售产品竞卖公告',1)[1].split('相关',1)[0]
    def field(pattern):
        m=re.search(pattern,text);return m[1].strip() if m else None
    product=field(r'产品名称：([^\n]+)')
    if product!='甲醇':raise ValueError('not a methanol auction')
    starting=field(r'起拍单价：([\d.]+)元/吨');planned=field(r'供货总量：([\d.]+)吨')
    timing=field(r'正式竞价启动时间：([^\n]+)')
    auction=dict(project_id=field(r'项目编号[：:]\s*([A-Z0-9-]+)'),direction='seller_sale',product=product,
        location=field(r'交货地点：([^\n]+)'),specification=field(r'质量标准：([^\n]+)'),
        auction_time_text=timing,starting_price=base.number(starting),clearing_price=None,
        planned_quantity=base.number(planned),actual_quantity=None,unsold_quantity=None,
        price_unit='CNY/t',quantity_unit='t',delivery_terms=field(r'提货方式：([^\n]+)'),result_status='planned_notice_only')
    result=event('auction','seller_notice/'+str(timing),'seller_auction_notice','announced',
        '新疆新业能化物流有限责任公司',publication_text=None,confidence='unverified_republication_lead',
        flags=['original_seller_result_unresolved','auction_timezone_unknown','not_a_transaction_result'],
        detail={'auction':auction,'excerpt':text[:1800],
                'does_not_establish_unsold_or_mto_demand':True})
    return [result]


def persist_events(db,source,attempt_id,evidence,events,processed_at):
    if not events:return 0
    if evidence.get('error_category') or evidence.get('http_status')!=200:raise ValueError('cannot persist events from failed response')
    first=base.timestamp(evidence['response_completed_at'] if evidence.get('response_completed_at') else evidence['ingested_at'])
    if first<base.timestamp(evidence['started_at']):raise ValueError('invalid response completion')
    processed=base.timestamp(processed_at); available=max(first,processed)
    inserted=0
    with db:
        for row in events:
            row_available=max(available,base.timestamp(row['published_at'])) if row['published_at'] else available
            previous=db.execute('SELECT * FROM industry_event WHERE source=? AND event_key=? ORDER BY id DESC LIMIT 1',
                                (source,row['event_key'])).fetchone()
            same=previous and all(previous[k]==(base.canonical(v).decode() if k=='detail' else v)
                                  for k,v in row.items())
            fingerprint=base.digest(base.canonical({'source':source,**row,
                                   'predecessor_id':previous['id'] if previous else None}))
            if same: event_id=previous['id']
            else:
                fields=['category','event_key','event_type','stage','actor','event_start','event_end','event_precision','published_at','publication_text']
                cur=db.execute('''INSERT INTO industry_event(source,category,event_key,event_type,stage,actor,
                 event_start,event_end,event_precision,published_at,publication_text,first_seen,processed_at,available_at,
                 availability_basis,confidence,quality,detail,raw_hash,attempt_id,fingerprint)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 [source,*[row[k] for k in fields],first.isoformat(),processed.isoformat(),row_available.isoformat(),
                  'first_seen_and_processed',row['confidence'],row['quality'],base.canonical(row['detail']).decode(),
                  evidence['raw_hash'],attempt_id,fingerprint])
                event_id=cur.lastrowid;inserted+=1
                if row['category']=='auction':
                    a=row['detail']['auction'];cols=list(a)
                    db.execute('INSERT INTO auction_fact(event_id,'+','.join(cols)+') VALUES('+','.join('?' for _ in range(len(cols)+1))+')',
                               [event_id,*[a[k] for k in cols]])
            db.execute('INSERT OR IGNORE INTO industry_event_evidence VALUES(?,?)',(event_id,attempt_id))
        db.execute("UPDATE ingest_attempt SET status='quarantined' WHERE id=?",(attempt_id,))
    return inserted


def visible_events(db,as_of):
    cutoff=base.timestamp(as_of);latest={}
    for r in db.execute('SELECT * FROM industry_event ORDER BY available_at,id'):
        r=dict(r)
        if base.timestamp(r['available_at'])<=cutoff:
            latest[(r['source'],r['event_key'])]=r
    return list(latest.values())


def ingest(db_path,raw_dir,through,names=None,timeout=20):
    day=date.fromisoformat(through); names=names or [*CATALOG,'cctd','cctd_scpi']
    if any(n not in CATALOG and n not in ('cctd','cctd_scpi') for n in names):raise ValueError('unknown industry source')
    db=connect(db_path);results=[]
    try:
        for name in names:
            if name in ('cctd','cctd_scpi'):
                results.extend(base.ingest(db_path,raw_dir,[through],[name],timeout)['attempts']);continue
            url=CATALOG[name][3]
            if name=='industry_portwatch':
                url+='?'+urlencode({'where':f"portid='chokepoint6' AND date <= DATE '{day}'",
                 'outFields':'date,portid,portname,n_total,n_tanker,capacity','orderByFields':'date DESC',
                 'resultRecordCount':7,'returnGeometry':'false','f':'json'})
            raw,e=base.acquire(url,raw_dir,timeout);rows=[];events=[]
            if not e.get('error_category'):
                try:
                    if name=='industry_hibor':rows=parse_report(raw)
                    elif name=='industry_mfa':events=parse_mfa(raw)
                    elif name=='industry_ukmto':events=parse_ukmto(raw)
                    elif name=='industry_portwatch':rows=parse_portwatch(raw)
                    elif name=='industry_auction_lead':events=parse_auction(raw)
                    elif name=='industry_oilchem':
                        text='\n'.join(base.page_lines(raw))
                        e.update(error_category='authentication_required' if '会员登录' in text else 'source_discovery_required',
                                 error='Public catalog/title is not licensed numeric content; stopped at member login')
                    else:e.update(error_category='source_discovery_required',error='PDF needs verified field-specific statistical dates; no inferred chart values')
                except (ValueError,KeyError,TypeError) as exc:e.update(error_category='parse_error',error=str(exc))
            if rows and any(r['observed_end'] and date.fromisoformat(r['observed_end'])>day for r in rows):
                rows=[];e.update(error_category='future_observation',error='response exceeds explicit through date')
            processed=base.utcnow()
            # Parsing availability cannot precede completion of this parsing pass.
            if rows:e['reprocessed_at']=processed
            e['industry_event_count']=len(events);e['through']=through
            result=base.persist(db,raw_dir,name,None,e,rows)
            result['event_inserted_count']=persist_events(db,name,result['attempt_id'],e,events,processed)
            if events:result['status']='quarantined'
            results.append(result)
    finally:db.close()
    return {'attempts':results,'inserted_observation_versions':sum(x['inserted_count'] for x in results),
            'inserted_event_versions':sum(x.get('event_inserted_count',0) for x in results),
            'errors':[x for x in results if x['status'] in ('failed','unavailable')],
            'forecast_enabled':False,'minimum_forecast_conditions_met':False}


def status(db_path,as_of=None):
    now=as_of or base.utcnow();cutoff=base.timestamp(now);db=connect(db_path)
    try:
        events=visible_events(db,now)
        facts={}
        for r in db.execute('''SELECT o.*,v.available_at FROM observation o JOIN observation_availability v
              ON v.observation_id=o.id ORDER BY o.id'''):
            if r['available_at'] and base.timestamp(r['available_at'])<=cutoff:
                facts[tuple(r[k] for k in ('source','metric','region','contract','unit','observed_start','observed_end'))]=dict(r)
        categories={k:{'current_numeric_fields':0,'missing_fields':0,'events':0} for k in ['inventory','mto','diplomacy','hormuz','coal','auction']}
        for r in facts.values():
            category=('inventory' if r['metric']=='port_inventory' else 'mto' if r['metric'] in
             ('mto_operating_rate','mto_external_operating_rate','mto_all_operating_rate') else
             'hormuz' if r['metric'].startswith('ais_') else 'coal' if r['source'] in ('cctd','cctd_scpi') else None)
            if category:categories[category]['current_numeric_fields' if r['value'] is not None else 'missing_fields']+=1
        for r in events:categories[r['category']]['events']+=1
        sources=[]
        for name in [*CATALOG,'cctd','cctd_scpi']:
            attempts=[dict(r) for r in db.execute('select * from ingest_attempt where source=? order by id',(name,))
                      if base.timestamp(r['ingested_at'])<=cutoff]
            source_facts=[r for r in facts.values() if r['source']==name]
            observed=max((r['observed_end'] for r in source_facts if r['observed_end']),default=None)
            report_dates=[]
            for r in source_facts:
                text=json.loads(r['detail']).get('publication_text') or ''
                if re.match(r'20\d{2}-\d{2}-\d{2}',text):report_dates.append(text[:10])
            sources.append({'source':name,'latest_attempt':attempts[-1] if attempts else None,
                'latest_observed_end':observed,'observed_age_days':(cutoff.date()-date.fromisoformat(observed)).days if observed else None,
                'report_date_not_observation_date':max(report_dates,default=None),
                'current_numeric_fields':sum(r['value'] is not None for r in source_facts),
                'period_unknown_fields':sum(not r['observed_start'] or not r['observed_end'] for r in source_facts),
                'license':'public_access_not_redistribution_permission'})
        return {'as_of':now,'categories':categories,'sources':sources,'visible_events':events,
          'observations':[r for r in facts.values() if r['source'] in CATALOG],
          'auction_verified_results':sum(r['category']=='auction' and
              r['confidence']=='original_seller_result' and r['stage']=='result' for r in events),
          'forecast_enabled':False,'minimum_forecast_conditions_met':False,
          'context_research_eligible_ais_fields':sum(r['source']=='industry_portwatch' and
              r['metric'] in ('ais_transits_total','ais_transits_tanker') and r['value'] is not None
              for r in facts.values()),
          'note':'Event visibility means the attributed report was locally known, not that its claim was independently confirmed. Unknown statistical periods stay quarantined; AIS is not methanol tonnage.'}
    finally:db.close()
