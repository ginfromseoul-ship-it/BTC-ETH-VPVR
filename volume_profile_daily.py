"""Approximate BTC/ETH Binance USD-M futures Volume Profile from official 5m kline archives.
No private data or API key. Fails closed on low coverage; NOT actual trade-by-trade VPVR.
"""
import io, json, math, os, time, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
import requests

BASE = 'https://data.binance.vision/data/futures/um'
NAMES = ['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore']
SYMBOLS={'BTCUSDT':100.0,'ETHUSDT':5.0}
INTERVAL='5m'
PERIODS={'30d':timedelta(days=30),'26w':timedelta(weeks=26),'52w':timedelta(weeks=52)}
S=requests.Session();S.headers.update({'User-Agent':'VPVR-research-v1'})


def download(url):
    err=None
    for retry in range(3):
        try:
            r=S.get(url,timeout=50)
            if r.status_code==404:return None
            r.raise_for_status()
            if not r.content.startswith(b'PK'):
                raise ValueError('response not a ZIP archive')
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                members=[x for x in z.namelist() if x.endswith('.csv')]
                if not members:raise ValueError('ZIP missing CSV')
                d=pd.read_csv(z.open(members[0]),header=None,names=NAMES,low_memory=False)
                d['open_time']=pd.to_numeric(d['open_time'],errors='coerce')
                d=d.dropna(subset=['open_time'])
                return d
        except Exception as exc:
            err=exc
            time.sleep(1.5*(retry+1))
    raise RuntimeError(f'{url}: {err}')


def months(a,b):
    y,m=a.year,a.month
    while (y,m)<=(b.year,b.month):
        yield y,m
        m+=1
        if m==13:y,m=y+1,1


def get_history(sym,start,end):
    pieces=[]; failures=[]
    lastday=end-timedelta(days=1)
    for y,m in months(start,lastday):
        first=datetime(y,m,1,tzinfo=timezone.utc)
        nextmonth=datetime(y+1,1,1,tzinfo=timezone.utc) if m==12 else datetime(y,m+1,1,tzinfo=timezone.utc)
        if nextmonth<=end and nextmonth<=datetime.now(timezone.utc)-timedelta(days=7):
            base=f'{sym}-5m-{y:04d}-{m:02d}.zip'
            u=f'{BASE}/monthly/klines/{sym}/5m/{base}'
            d=download(u)
            if d is None:failures.append(u)
            else:pieces.append(d)
        else:
            d0=max(first,start.replace(hour=0,minute=0,second=0,microsecond=0))
            d1=min(nextmonth,end)
            current=d0
            while current<d1:
                basename=f'{sym}-5m-{current:%Y-%m-%d}.zip'
                u=f'{BASE}/daily/klines/{sym}/5m/{basename}'
                d=download(u)
                if d is None:failures.append(u)
                else:pieces.append(d)
                current+=timedelta(days=1)
    if failures:raise RuntimeError(f'{sym}: missing {len(failures)} required archive files, e.g. {failures[:2]}')
    if not pieces:raise RuntimeError(f'{sym}: no data loaded')
    out=pd.concat(pieces,ignore_index=True)
    for c in ['high','low','close','volume']:
        out[c]=pd.to_numeric(out[c],errors='coerce')
    out['dt']=pd.to_datetime(out['open_time'],unit='ms',utc=True,errors='coerce')
    out=out.dropna(subset=['dt','high','low','close','volume']).drop_duplicates('dt')
    out=out.loc[(out.dt>=start)&(out.dt<end)].sort_values('dt').copy()
    expected=int((end-start).total_seconds()/300)
    coverage=len(out)/expected
    if coverage<0.97:
        raise RuntimeError(f'{sym}: candle coverage {coverage:.1%} too low (minimum 97%); data omitted')
    return out,coverage


def compute_profile(d,width):
    hist={}
    for lo,hi,close,vol in d[['low','high','close','volume']].itertuples(index=False,name=None):
        if vol<=0 or not np.isfinite(vol):continue
        lo,hi=min(lo,hi),max(lo,hi)
        if lo==hi:
            k=math.floor(close/width);hist[k]=hist.get(k,0)+vol
            continue
        for k in range(math.floor(lo/width), math.floor(hi/width)+1):
            overlap=max(0.,min(hi,(k+1)*width)-max(lo,k*width))
            if overlap:hist[k]=hist.get(k,0.)+vol*overlap/(hi-lo)
    if not hist:raise RuntimeError('no volume')
    keys=np.array(sorted(hist),dtype=int); v=np.array([hist[k] for k in keys]);p=(keys+.5)*width
    poc=int(np.argmax(v));left=right=poc;total=v.sum();area=v[poc]
    while area/total<.70 and (left>0 or right<len(v)-1):
        a=v[left-1] if left else -1;b=v[right+1] if right<len(v)-1 else -1
        if a>=b:left-=1;area+=v[left]
        else:right+=1;area+=v[right]
    smooth=pd.Series(v).rolling(5,center=True,min_periods=1).mean().values
    peaks=[i for i in range(1,len(v)-1) if smooth[i]>=smooth[i-1] and smooth[i]>smooth[i+1]]
    prominent=sorted(sorted(peaks,key=lambda i:smooth[i],reverse=True)[:4]);valleys=[]
    for a,b in zip(prominent,prominent[1:]):
        if b-a>=3:valleys.append(round(float(p[a+1+int(np.argmin(smooth[a+1:b]))]),2))
    return {'most_traded_price_POC_usd':float(p[poc]),
            'volume_concentrated_HVN_centers_usd':[round(float(p[i]),2) for i in prominent],
            'volume_thin_LVN_centers_usd':valleys,
            'value_area_high_VAH_usd':float((keys[right]+1)*width),
            'value_area_low_VAL_usd':float(keys[left]*width),
            'value_area_fraction':float(area/total),
            'bin_width_usd':width,'latest_close_usd':float(d.close.iloc[-1]),
            'latest_price_position':('above value area' if d.close.iloc[-1]>(keys[right]+1)*width
                                  else 'below value area' if d.close.iloc[-1]<keys[left]*width
                                  else 'inside value area')}


def choose_latest_available_end(requested_end, max_lag_days=4):
    """Use the newest shared BTC/ETH *published* full UTC day.

    Daily ZIPs are not published instantly after 00:00 UTC. A missing latest
    archive (HTTP 404) is a publication lag, not proof of a broken network.
    Never silently use data older than max_lag_days.
    """
    for lag in range(max_lag_days + 1):
        candidate_end = requested_end - timedelta(days=lag)
        last_day = candidate_end - timedelta(days=1)
        missing_symbols = []
        for symbol in SYMBOLS:
            basename = f'{symbol}-5m-{last_day:%Y-%m-%d}.zip'
            url = f'{BASE}/daily/klines/{symbol}/5m/{basename}'
            if download(url) is None:
                missing_symbols.append(symbol)
        if not missing_symbols:
            print(f'Using complete UTC data through {last_day:%Y-%m-%d}; archive publication lag: {lag} day(s)')
            return candidate_end, lag
        print(f'Archive not yet published for {last_day:%Y-%m-%d}: {", ".join(missing_symbols)}; trying previous day')
    raise RuntimeError(f'No complete BTC and ETH daily archive pair found within {max_lag_days} days; refuse stale results')


def run(end=None):
    user_supplied_end = end is not None
    end = end or datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    if end.tzinfo is None:end=end.replace(tzinfo=timezone.utc)
    requested_end = end
    publication_lag = 0
    if not user_supplied_end:
        end, publication_lag = choose_latest_available_end(end)
    start=end-PERIODS['52w']
    anchor=os.environ.get('ANCHOR_UTC','').strip()
    windows=dict(PERIODS)
    if anchor:
        ad=datetime.fromisoformat(anchor).replace(tzinfo=timezone.utc)
        if ad<start or ad>=end:
            raise ValueError(f'ANCHOR_UTC must be within last 52 weeks {start}..{end}')
        windows['anchored']=end-ad
    result={'generated_utc':datetime.now(timezone.utc).isoformat(),
            'data_end_utc_exclusive':end.isoformat(),
            'requested_end_utc_exclusive':requested_end.isoformat(),
            'archive_publication_lag_days':publication_lag,
            'source':'Binance USD-M perpetual BTCUSDT ETHUSDT official 5m kline archives',
            'method':'Uniform distribution of each 5-minute bar volume across high-low bins; approximation, NOT TradingView VPVR or actual tick profile',
            'profiles':{}}
    for sym,width in SYMBOLS.items():
        data,coverage=get_history(sym,start,end)
        for window,span in windows.items():
            sliced=data.loc[data.dt>=end-span]
            expected=int(span.total_seconds()/300)
            if len(sliced)/expected<.97:
                raise RuntimeError(f'{sym} {window}: incomplete profile {len(sliced)}/{expected}')
            stats=compute_profile(sliced,width)
            stats.update({'symbol':sym,'window':window,'window_start_utc':(end-span).isoformat(),
                          'number_of_5m_bars':int(len(sliced)),'coverage':float(len(sliced)/expected)})
            result['profiles'][f'{sym}_{window}']=stats
    Path('data').mkdir(exist_ok=True)
    p=Path('data/volume_profile_summary.json')
    p.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print('Saved',p, 'profiles:', list(result['profiles']))
    return result

if __name__=='__main__':run()
