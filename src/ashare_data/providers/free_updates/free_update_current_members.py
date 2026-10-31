"""Current exchange ETF directories and explicitly selected industry members."""
from io import BytesIO, StringIO
import json
import re
from urllib.parse import urlencode


def szse_etfs(capture, params):
    import pandas as pd
    if params:
        raise ValueError("SZSE current ETF file accepts no security filter")
    url = 'https://fund.szse.cn/api/report/ShowReport?' + urlencode({
        'SHOWTYPE':'xlsx', 'CATALOGID':'1000_lf', 'TABKEY':'tab1', 'random':'0.07610353191740105'})
    frame = pd.read_excel(BytesIO(capture.get(url, {
        'Referer':'https://fund.szse.cn/marketdata/fundslist/index.html'})),
        engine='openpyxl', dtype={'基金代码':str})
    fields = ['基金代码','基金简称','基金类别','投资类别','上市日期','基金份额',
              '基金管理人','基金发起人','基金托管人','净值']
    frame = frame.rename(columns={'当前规模(份)':'基金份额'})
    if frame.columns.has_duplicates or not set(fields) <= set(frame.columns) or frame['基金类别'].isna().any():
        raise ValueError("SZSE fund identity/classification fields changed")
    counts = {str(k):int(v) for k,v in frame['基金类别'].value_counts().items()}
    total = len(frame)
    frame = frame.loc[frame['基金类别']=='ETF',fields].copy()
    if frame.empty or frame['基金代码'].duplicated().any():
        raise ValueError("SZSE current ETF file empty or has repeated codes")
    frame['上市日期'] = pd.to_datetime(frame['上市日期'], errors='raise').dt.date
    frame['基金份额'] = pd.to_numeric(frame['基金份额'].astype(str).str.replace(',','',regex=False), errors='raise')
    frame['净值'] = pd.to_numeric(frame['净值'], errors='coerce')
    rows = json.loads(frame.to_json(orient='records',date_format='iso',force_ascii=False))
    capture.dataset_metadata = {'source':'SZSE official current fund workbook', 'source_count':len(rows),
        'complete_returned_window':True,'complete_universe':True,'exchange':'SZSE',
        'source_fund_rows':total,'source_type_counts':counts,'selected_etf_rows':len(rows),
        'excluded_non_etf_rows':total-len(rows),'share_statistical_date':None,
        'source_window':'all explicitly ETF-classified current rows; retain each actual received snapshot',
        'units':{'基金份额':'shares','净值':'CNY/share'},
        'date_role':'listing date is not a share-statistics date; missing statistics date stays null',
        'historical_api_required':False}
    return rows


def sw_members(capture, params, *, level=3):
    import pandas as pd
    from bs4 import BeautifulSoup
    if level not in {1,2,3} or set(params)!={'symbol'} or not re.fullmatch(r'\d{6}\.SI',str(params['symbol'])):
        raise ValueError("explicit SW industry identity required")
    url = 'https://legulegu.com/stockdata/index-composition?industryCode='+params['symbol']
    body = capture.get(url)
    soup = BeautifulSoup(body,'lxml')
    title = soup.title.get_text(strip=True) if soup.title else ''
    match = re.fullmatch(r'(.+?)\('+re.escape(params['symbol'])+r'\)申万行业成分股.*',title)
    if not match:
        raise ValueError("industry source title does not echo requested code")
    tables = pd.read_html(StringIO(body.decode('utf-8')))
    classification = f'申万{level}级'
    candidates = [f for f in tables if {'股票代码','股票简称',classification} <= set(f.columns)]
    if len(candidates)!=1:
        raise ValueError("current industry membership table absent or ambiguous")
    frame = candidates[0].copy()
    frame.columns = [re.sub(r'\s*\{.*','',str(c),flags=re.S).strip().replace('市值（亿元）','市值') for c in frame.columns]
    if frame.columns.has_duplicates or frame.empty:
        raise ValueError("industry member columns repeated or empty")
    for field in ['序号','价格','市盈率','市盈率ttm','市净率','市值']:
        frame[field] = pd.to_numeric(frame[field],errors='coerce')
    for field in frame.columns:
        if field in {'股息率','ROE(%)'} or '涨幅' in field or '(%)' in field:
            frame[field] = pd.to_numeric(frame[field].astype(str).str.strip('%'),errors='coerce')
    rows = json.loads(frame.to_json(orient='records',date_format='iso',force_ascii=False))
    if any(row[classification]!=match[1] for row in rows):
        raise ValueError("industry members belong to another classification")
    description = soup.find('meta',attrs={'name':'description'})
    date = re.match(r'\d{4}-\d{2}-\d{2}',description.get('content','')) if description else None
    capture.dataset_metadata = {'source':url,'source_count':len(rows),'complete_returned_window':True,
        'industry_code':params['symbol'],'industry_name':match[1],
        'source_page_date':date[0] if date else None,'whole_industry_directory':False,
        'source_window':'complete current static member table for the explicitly selected industry',
        'historical_api_required':False,'unknown_native_fields_retained':True,
        'inclusion_date_meaning':'native 纳入时间 preserved; not asserted to be SW-classification entry date',
        'units':{'市值':'100000000 CNY','价格':'CNY/share'}}
    if level != 3:
        capture.dataset_metadata['industry_level'] = level
    return rows
