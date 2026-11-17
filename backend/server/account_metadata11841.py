"""Owner-only account metadata and server-confirmed foreground usage, never AI context."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
import ipaddress
from uuid import UUID
from fastapi import APIRouter,Depends,HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from .platform_auth import authentication_dependency

META='member_account_metadata11841'
SESSIONS='member_usage_sessions11841'

def stamp(value):return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
def parsed(value):return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(timezone.utc)
def utc_text(value):
    return None if value is None else stamp(value if isinstance(value,datetime)else parsed(value))

class Heartbeat(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)
    session_id:str=Field(min_length=36,max_length=36)
    event_id:str=Field(min_length=36,max_length=36)
    sequence:int=Field(ge=0,le=9007199254740991)
    foreground:bool

class AccountMetadataService:
    def __init__(self,auth,database,*,clock=None):
        self.auth,self.db=auth,database;self.clock=clock or(lambda:datetime.now(timezone.utc))
    @staticmethod
    def actor(principal):
        if principal.user.role_code!='member'or type(principal.user.id)is not int:raise HTTPException(403,'仅本人会员可读取账户信息')
        return principal.user.id
    def record_login(self,connection,actor,client_ip):
        try:value=str(ipaddress.ip_address(client_ip))if client_ip else None
        except (ValueError,TypeError):value=None
        now=stamp(self.clock())
        connection.execute('INSERT INTO '+META+'(user_id,last_login_ip,foreground_ms,last_credited_at,usage_updated_at,updated_at) VALUES(?,?,0,NULL,NULL,?) '
                           'ON CONFLICT(user_id) DO UPDATE SET last_login_ip=excluded.last_login_ip,updated_at=excluded.updated_at',(actor,value,now))
    def metadata(self,principal):
        actor=self.actor(principal)
        with self.auth.identity(principal),self.db.connect()as conn:
            # Hash bytes never leave the database or enter the returned packet.
            row=conn.execute('SELECT u.created_at,(length(u.password_hash)>0 AND COALESCE(c.credentials_set,1)=1) AS password_configured '
                'FROM users u LEFT JOIN phone_auth_credentials11838 c ON c.user_id=u.id WHERE u.id=?',(actor,)).fetchone()
            data=conn.execute('SELECT last_login_ip,foreground_ms,usage_updated_at FROM '+META+' WHERE user_id=?',(actor,)).fetchone()
            memberships=conn.execute('SELECT plan_code,status,starts_at,ends_at FROM memberships WHERE user_id=? ORDER BY id DESC LIMIT 100',(actor,)).fetchall()
        if row is None:raise HTTPException(401,'账号已变化')
        return {'actor_id':actor,'account_created_at':utc_text(row['created_at']),'password_configured':bool(row['password_configured']),
                'last_login_ip':data['last_login_ip']if data else None,'foreground_ms':int(data['foreground_ms'])if data else 0,
                'usage_updated_at':utc_text(data['usage_updated_at'])if data else None,
                'memberships':[{key:utc_text(item[key])if key in('starts_at','ends_at')else str(item[key])for key in('plan_code','status','starts_at','ends_at')}for item in memberships]}
    def heartbeat(self,principal,body):
        actor=self.actor(principal);value=body.model_dump()if isinstance(body,Heartbeat)else Heartbeat.model_validate(body).model_dump()
        for key in('session_id','event_id'):
            try:
                if str(UUID(value[key]))!=value[key]:raise ValueError
            except (ValueError,TypeError,AttributeError):raise HTTPException(422,'使用记录编号无效')from None
        now=self.clock().astimezone(timezone.utc);at=stamp(now)
        with self.auth.identity(principal),self.db.transaction()as conn:
            conn.execute('INSERT INTO '+META+'(user_id,foreground_ms,updated_at) VALUES(?,0,?) ON CONFLICT(user_id) DO NOTHING',(actor,at))
            account=conn.execute('SELECT foreground_ms,last_credited_at FROM '+META+' WHERE user_id=?',(actor,)).fetchone()
            previous=conn.execute('SELECT event_id,sequence_no,foreground,last_seen_at FROM '+SESSIONS+' WHERE user_id=? AND session_id=?',(actor,value['session_id'])).fetchone()
            if previous and(value['sequence']<=previous['sequence_no']or value['event_id']==previous['event_id']):
                return {'actor_id':actor,'foreground_ms':int(account['foreground_ms']),'last_seen_at':utc_text(previous['last_seen_at']),'counted_ms':0,'replayed':True}
            if not previous:
                conn.execute('DELETE FROM '+SESSIONS+' WHERE user_id=? AND last_seen_at<?',(actor,stamp(now-timedelta(days=7))))
                count=conn.execute('SELECT COUNT(*) AS n FROM '+SESSIONS+' WHERE user_id=?',(actor,)).fetchone()['n']
                if count>=16:raise HTTPException(429,'活动会话过多，请稍后再试')
            counted=0
            if previous and bool(previous['foreground']):
                last=parsed(previous['last_seen_at']);elapsed=(now-last).total_seconds()
                if 0<=elapsed<=45:
                    start=max(last,parsed(account['last_credited_at']))if account['last_credited_at']else last
                    counted=max(0,min(30000,int((now-start).total_seconds()*1000)))
            total=int(account['foreground_ms'])+counted
            conn.execute('INSERT INTO '+SESSIONS+'(user_id,session_id,event_id,sequence_no,foreground,last_seen_at) VALUES(?,?,?,?,?,?) '
                         'ON CONFLICT(user_id,session_id) DO UPDATE SET event_id=excluded.event_id,sequence_no=excluded.sequence_no,foreground=excluded.foreground,last_seen_at=excluded.last_seen_at',
                         (actor,value['session_id'],value['event_id'],value['sequence'],int(value['foreground']),at))
            if counted:
                conn.execute('UPDATE '+META+' SET foreground_ms=?,last_credited_at=?,usage_updated_at=?,updated_at=? WHERE user_id=?',(total,at,at,at,actor))
            return {'actor_id':actor,'foreground_ms':total,'last_seen_at':at,'counted_ms':counted,'replayed':False}

def create_account_metadata_router(service):
    router=APIRouter(prefix='/v1/member',tags=['member']);principal=Depends(authentication_dependency(service.auth))
    @router.get('/account-metadata')
    def metadata(identity=principal):return JSONResponse(service.metadata(identity),headers={'Cache-Control':'no-store'})
    @router.post('/usage/heartbeat')
    def heartbeat(value:Heartbeat,identity=principal):return JSONResponse(service.heartbeat(identity,value),headers={'Cache-Control':'no-store'})
    return router
