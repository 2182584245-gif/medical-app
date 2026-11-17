"""Next local notification dates over real reminder rows, with no network access."""
from __future__ import annotations
import calendar
from datetime import datetime,timedelta,timezone

# The mobile reminder editor and list both use Beijing time. Stored rows are
# normalized to UTC, so calendar rules must first restore that same wall clock.
_WALL_ZONE=timezone(timedelta(hours=8))
_DELIVERY_GRACE=timedelta(minutes=15)


def _stamp(value):
    result=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Reminder timezone missing')
    return result


def next_due(row,now=None):
    now=_stamp(now or datetime.now(timezone.utc)).astimezone(_WALL_ZONE)
    if not isinstance(row,dict) or row.get('status','active')!='active' or row.get('enabled') is False:return None
    base=_stamp(row['scheduled_at']).astimezone(_WALL_ZONE);repeat=row.get('repeat_rule')
    if repeat is None or repeat=='':repeat='none'
    if type(repeat) is not str:return None
    if repeat not in {'none','daily','weekly','monthly'}:return None
    floor=now
    if row.get('paused_until'):floor=max(floor,_stamp(row['paused_until']).astimezone(_WALL_ZONE))
    if base>=floor:return base
    if repeat=='none':return None
    if repeat in {'daily','weekly'}:
        interval=timedelta(days=1 if repeat=='daily' else 7)
        steps=(floor-base)//interval
        candidate=base+max(0,steps)*interval
        return candidate if candidate>=floor else candidate+interval
    if repeat=='monthly':
        months=max(0,(floor.year-base.year)*12+floor.month-base.month)
        for offset in (months,months+1):
            year,month=divmod(base.year*12+base.month-1+offset,12);month+=1
            candidate=base.replace(year=year,month=month,day=min(base.day,calendar.monthrange(year,month)[1]))
            if candidate>=floor:return candidate
    return None


def schedule(rows,now=None):
    result=[]
    for row in rows:
        if not isinstance(row,dict) or type(row.get('id')) is not int or row['id']==0:continue
        try:due=next_due(row,now)
        except (ValueError,TypeError,KeyError,OverflowError):continue
        if due is not None:result.append({'id':str(row['id']),'at':round(due.timestamp()*1000)})
    return sorted(result,key=lambda row:(row['at'],row['id']))[:512]


def delivery(rows,identifier,at,now=None):
    if type(identifier) is not str or type(at) is not int or at<=0:return None
    row=next((item for item in rows if isinstance(item,dict) and str(item.get('id'))==identifier),None)
    if row is None:return None
    try:
        current=_stamp(now or datetime.now(timezone.utc))
        occurrence=datetime.fromtimestamp(at/1000,timezone.utc)
        if current<occurrence or current-occurrence>_DELIVERY_GRACE:return None
        expected=next_due(row,datetime.fromtimestamp(at/1000,timezone.utc)-timedelta(milliseconds=1))
        if expected is None or abs(round(expected.timestamp()*1000)-at)>1:return None
        title=row.get('title')
        if not isinstance(title,str) or not 1<=len(title)<=200:return None
        return title
    except (ValueError,TypeError,KeyError,OverflowError):return None
