"""Bounded validation and transaction helpers for the fixed member API."""
from __future__ import annotations
import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

class ExpansionError(ValueError):
    def __init__(self, code, message=None):
        self.code=code
        super().__init__(message or code)

def fail(code="invalid_request"):
    raise ExpansionError(code)

def body(value, allowed, required=()):
    if type(value) is not dict or set(value)-set(allowed) or not set(required)<=set(value): fail()
    return value

def text(value, maximum=4000, optional=False):
    if optional and value is None: return None
    if type(value) is not str or len(value)>maximum or (not optional and not value.strip()): fail()
    return value.strip()

def integer(value):
    if type(value) is not int or value<1 or value>2**63-1: fail()
    return value

def number(value, low=0, high=1e6, nullable=False):
    if value is None and nullable: return None
    if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high: fail()
    return value

def boolean(value):
    if type(value) is not bool: fail()
    return value

def choice(value, values):
    if type(value) is not str or value not in values: fail()
    return value

def iso(value):
    value=text(value,80)
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError: fail()
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.year<1900: fail()
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00","Z")

def day(value):
    if type(value) is not str or len(value)!=10: fail()
    try: parsed=date.fromisoformat(value)
    except ValueError: fail()
    if parsed.year<1900: fail()
    return parsed.isoformat()

def now():
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00","Z")

def today(): return datetime.now(BEIJING).date().isoformat()

def dumps(value):
    try: result=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    except (ValueError,TypeError): fail()
    if len(result.encode())>110000: fail("payload_too_large")
    return result

def version(value):
    return hashlib.sha256(dumps({k:v for k,v in value.items() if k!="version"}).encode()).hexdigest()

def entity(row):
    value=dict(row)
    payload=value.pop("payload_json",None)
    if payload: value.update(json.loads(payload))
    value["version"]=version(value)
    return value

def match(current, supplied):
    if type(supplied) is not str or current["version"]!=supplied: fail("conflict")

def pagination(payload):
    limit=payload.get("limit",20); offset=payload.get("offset",0)
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=100000: fail()
    return limit,offset

class Base:
    def _actor(self, actor):
        actor=integer(actor)
        with self.database.connect() as c:
            row=c.execute("SELECT role_code,account_status FROM users WHERE id=?",(actor,)).fetchone()
        if not row or row["role_code"]!="member" or row["account_status"]!="active": fail("permission_denied")
        return actor

    def _get(self, table, actor, item_id):
        # table names are private constants, never request-controlled.
        with self.database.connect() as c:
            row=c.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?",(integer(item_id),actor)).fetchone()
        if row is None: fail("not_found")
        return entity(row)

    def _page(self, table, actor, payload, filters=None, order="id DESC"):
        limit,offset=pagination(payload)
        sql=f" FROM {table} WHERE user_id=?"; args=[actor]
        for field,value in (filters or {}).items():
            if value is not None: sql+=f" AND {field}=?"; args.append(value)
        with self.database.connect() as c:
            total=c.execute("SELECT COUNT(*) AS n"+sql,args).fetchone()["n"]
            rows=c.execute("SELECT *"+sql+f" ORDER BY {order} LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
        return {"items":[entity(r) for r in rows],"total":total,"limit":limit,"offset":offset}

    def _insert(self, table, actor, fields, payload):
        stamp=now(); columns=["user_id",*fields,"payload_json","created_at","updated_at"]
        values=[actor,*fields.values(),dumps(payload),stamp,stamp]
        with self.database.transaction() as c:
            cursor=c.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in values)})",values)
            item_id=int(cursor.lastrowid)
        return self._get(table,actor,item_id)

    def _replace(self, table, actor, current, fields, payload):
        fields={**fields,"payload_json":dumps(payload),"updated_at":now()}
        with self.database.transaction() as c:
            c.execute(f"UPDATE {table} SET "+",".join(f"{key}=?" for key in fields)+" WHERE id=? AND user_id=?",(*fields.values(),current["id"],actor))
        return self._get(table,actor,current["id"])

    def _revision(self,c,actor,kind,item_id,operation,before,after,reason):
        c.execute("INSERT INTO expansion_record_revisions(user_id,entity_type,entity_id,operation,before_json,after_json,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",(actor,kind,item_id,operation,None if before is None else dumps(before),None if after is None else dumps(after),text(reason,500),now()))
