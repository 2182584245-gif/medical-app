from __future__ import annotations
import json
import re
import secrets
from datetime import date,timedelta
from .common import *

POINT_RULES=[{"event":"habit","points":10,"daily_cap":30,"notice":"仅当日打卡，取消追加冲正；重复打卡不再奖励"},{"event":"reading","points":3,"daily_cap":9,"notice":"每篇首次完成，客户端上报阅读进度，不代表真实阅读证明"},{"event":"meal","points":2,"daily_cap":8},{"event":"observation","points":2,"daily_cap":4},{"event":"invite","points":20,"notice":"被邀请人首次本人确认餐食或身体指标后生效；无现金价值"}]

class Member:
    def _memory_value(self,payload):
        return {"key":text(payload["key"],100),"value":text(payload["value"],2000),"source_ref":text(payload.get("source_ref"),100,True) or None,"confirmed_at":None}

    def create_memory(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("key","value","kind","source_ref"),("key","value")); data=self._memory_value(payload)
        if data["source_ref"]: self._validate_refs(actor,[data["source_ref"]])
        return self._insert("expansion_memories",actor,{"memory_key":data["key"],"kind":choice(payload.get("kind","preference"),{"preference","routine","explicit_fact"}),"status":"pending"},data)

    def list_memories(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("status","limit","offset")); status=payload.get("status")
        if status is not None: choice(status,{"pending","confirmed","revoked"})
        return self._page("expansion_memories",actor,payload,{"status":status})

    def update_memory(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("memory_id","base_version","value"),("memory_id","base_version","value"))
        with self.database.transaction():
            current=self._get("expansion_memories",actor,payload["memory_id"]); match(current,payload["base_version"])
            result=self._replace("expansion_memories",actor,current,{"status":"pending"},{"key":current["key"],"value":text(payload["value"],2000),"source_ref":current["source_ref"],"confirmed_at":None})
            with self.database.connect() as c: self.health._add_audit_log(c,actor,"expansion.memory.updated","expansion_memory",current["id"])
            return result

    def _memory_state(self,actor,payload,status):
        actor=self._actor(actor); body(payload,("memory_id","base_version"),("memory_id","base_version"))
        with self.database.transaction():
            current=self._get("expansion_memories",actor,payload["memory_id"]); match(current,payload["base_version"])
            if status=="confirmed" and current["status"]!="pending": fail("invalid_state")
            result=self._replace("expansion_memories",actor,current,{"status":status},{"key":current["key"],"value":current["value"],"source_ref":current["source_ref"],"confirmed_at":now() if status=="confirmed" else current.get("confirmed_at")})
            with self.database.connect() as c: self.health._add_audit_log(c,actor,"expansion.memory."+status,"expansion_memory",current["id"])
            return result

    def confirm_memory(self,actor_user_id,payload): return self._memory_state(actor_user_id,payload,"confirmed")
    def revoke_memory(self,actor_user_id,payload): return self._memory_state(actor_user_id,payload,"revoked")

    def delete_memory(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("memory_id","base_version"),("memory_id","base_version"))
        with self.database.transaction() as c:
            current=self._get("expansion_memories",actor,payload["memory_id"]); match(current,payload["base_version"])
            # No plaintext copy in audit/revisions; references in already-produced answers cannot be retracted.
            c.execute("DELETE FROM expansion_memories WHERE id=? AND user_id=?",(current["id"],actor))
            self.health._add_audit_log(c,actor,"expansion.memory.deleted","expansion_memory",current["id"])
        return {"deleted":True,"memory_id":current["id"]}

    def _observation_data(self,payload,current=None):
        from .mobile_observation_schema import observation_data
        return observation_data(payload,current)

    def create_observation(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("kind","value","unit","measured_at","context","notes","report_item_id","diastolic","metric_name"),("kind","value","unit","measured_at"))
        with self.database.transaction() as c:
            data=self._observation_data(payload); measured=iso(payload["measured_at"])
            if data["report_item_id"] is not None:
                row=c.execute("SELECT i.id FROM report_items i JOIN medical_reports r ON r.id=i.report_id WHERE i.id=? AND r.user_id=? AND i.status='confirmed' AND r.status='confirmed'",(integer(data["report_item_id"]),actor)).fetchone()
                if row is None: fail("report_item_not_confirmed")
            item=self._insert("expansion_observations",actor,{"kind":payload["kind"],"measured_at":measured},data)
            self._revision(c,actor,"observation",item["id"],"create",None,item,"本人确认身体指标")
            self._award_points(c,actor,"observation:"+str(item["id"]),2,"身体指标记录",daily_limit=4)
            self._qualify_invite(c,actor)
        return item

    def get_observation(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("observation_id",),("observation_id",)); return self._get("expansion_observations",actor,payload["observation_id"])

    def list_observations(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("kind","start_date","end_date","limit","offset")); kind=payload.get("kind")
        if kind is not None: choice(kind,{"weight","glucose","blood_pressure","lipids","height","custom"})
        return self._dated_page("expansion_observations",actor,payload,"measured_at",{"kind":kind})

    def update_observation(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("observation_id","base_version","value","unit","measured_at","context","notes","reason","diastolic","metric_name"),("observation_id","base_version","reason"))
        with self.database.transaction() as c:
            current=self._get("expansion_observations",actor,payload["observation_id"]); match(current,payload["base_version"])
            data=self._observation_data(payload,current)
            item=self._replace("expansion_observations",actor,current,{"measured_at":iso(payload.get("measured_at",current["measured_at"]))},data)
            self._revision(c,actor,"observation",current["id"],"update",current,item,payload["reason"])
        return item

    def delete_observation(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("observation_id","base_version","reason"),("observation_id","base_version","reason"))
        with self.database.transaction() as c:
            current=self._get("expansion_observations",actor,payload["observation_id"]); match(current,payload["base_version"])
            self._revision(c,actor,"observation",current["id"],"delete",current,None,payload["reason"])
            self._reverse_points(c,actor,"observation:"+str(current["id"]),"删除身体指标")
            c.execute("DELETE FROM expansion_observations WHERE id=? AND user_id=?",(current["id"],actor))
        return {"deleted":True,"observation_id":current["id"]}

    def _goal_data(self,payload,current=None):
        merged={**(current or {}),**payload}; title=text(merged["title"],200); target=number(merged.get("target"),0.01,100000,True)
        frequency=choice(merged.get("frequency","daily"),{"daily","weekly"}); reminder=merged.get("reminder_time")
        if reminder is not None and (type(reminder) is not str or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",reminder)): fail()
        return {"title":title,"target":target,"unit":text(merged.get("unit"),30,True),"frequency":frequency,"reminder_time":reminder}

    def create_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("title","kind","target","unit","frequency","reminder_time"),("title","kind"))
        kind=choice(payload["kind"],{"water","diet","weight","glucose","reading","custom"}); data=self._goal_data(payload)
        with self.database.transaction():
            data['reminder_id']=self._sync_goal_reminder(actor,data)
            result=self._insert("expansion_goals",actor,{"kind":kind,"status":"active"},data)
        return self.get_goal(actor,{"goal_id":result["id"]})

    def _sync_goal_reminder(self,actor,data,existing_id=None,paused=False):
        with self.database.connect() as c:
            existing=c.execute('SELECT id FROM reminders WHERE id=? AND user_id=?',(existing_id,actor)).fetchone() if existing_id is not None else None
        if existing is None: existing_id=None
        if paused or data.get('reminder_time') is None:
            if existing_id is not None: self.health.pause_reminder(actor,existing_id)
            return existing_id
        scheduled=datetime.fromisoformat(today()+'T'+data['reminder_time']+':00+08:00')
        if scheduled<=datetime.now(BEIJING): scheduled+=timedelta(days=1)
        title='目标：'+data['title'][:197]
        if existing_id is None:
            return self.health.add_reminder(actor,title,scheduled,reminder_type='custom',repeat_rule=data['frequency'])
        self.health.update_reminder(actor,existing_id,title=title,scheduled_at=scheduled,reminder_type='custom',repeat_rule=data['frequency'])
        self.health.resume_reminder(actor,existing_id)
        return existing_id

    def get_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("goal_id",),("goal_id",)); result=self._get("expansion_goals",actor,payload["goal_id"])
        with self.database.connect() as c:
            rows=c.execute("SELECT id,day,note,created_at FROM expansion_habit_entries WHERE goal_id=? AND user_id=? ORDER BY day DESC LIMIT 366",(result["id"],actor)).fetchall()
        result["entries"]=[dict(row) for row in rows]; result["progress"]={"checkin_count":len(rows),"today_checked":any(r["day"]==today() for r in rows)}
        if result.get('reminder_id') is not None:
            with self.database.connect() as c: linked=c.execute('SELECT status FROM reminders WHERE id=? AND user_id=?',(result['reminder_id'],actor)).fetchone()
            result['reminder_status']=linked['status'] if linked else 'missing'
        else: result['reminder_status']='unset'
        # Entity version excludes derived entries, so a check-in doesn't invalidate goal editing.
        return result

    def list_goals(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("status","limit","offset")); status=payload.get("status")
        if status is not None: choice(status,{"active","archived"})
        result=self._page("expansion_goals",actor,payload,{"status":status}); result["items"]=[self.get_goal(actor,{"goal_id":v["id"]}) for v in result["items"]]
        return result

    def update_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("goal_id","base_version","changes"),("goal_id","base_version","changes")); changes=body(payload["changes"],("title","target","unit","frequency","reminder_time"))
        with self.database.transaction():
            current=self._get("expansion_goals",actor,payload["goal_id"]); match(current,payload["base_version"])
            if current["status"]!="active": fail("invalid_state")
            data=self._goal_data(changes,current); data['reminder_id']=self._sync_goal_reminder(actor,data,current.get('reminder_id'))
            self._replace("expansion_goals",actor,current,{},data)
        return self.get_goal(actor,{"goal_id":current["id"]})

    def close_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("goal_id","base_version"),("goal_id","base_version"))
        with self.database.transaction():
            current=self._get("expansion_goals",actor,payload["goal_id"]); match(current,payload["base_version"])
            data=self._goal_data({},current); data['reminder_id']=self._sync_goal_reminder(actor,data,current.get('reminder_id'),paused=True)
            self._replace("expansion_goals",actor,current,{"status":"archived"},data)
        return self.get_goal(actor,{"goal_id":current["id"]})

    def delete_goal(self,actor_user_id,payload):
        """Erase one owned goal and its business children in the same transaction."""
        actor=self._actor(actor_user_id)
        body(payload,("goal_id","base_version"),("goal_id","base_version"))
        with self.database.transaction() as c:
            current=self._get("expansion_goals",actor,payload["goal_id"])
            match(current,payload["base_version"])
            goal_id=current['id']
            entries=c.execute('SELECT id FROM expansion_habit_entries WHERE goal_id=? AND user_id=?',(goal_id,actor)).fetchall()
            invalidate=getattr(self,'goal_memory_invalidator',None)
            if callable(invalidate):invalidate(c,actor,goal_id,[row['id']for row in entries])
            reminder_ids=[]
            reminder=current.get('reminder_id')
            if reminder is not None:
                owned=c.execute('SELECT id FROM reminders WHERE id=? AND user_id=?',(reminder,actor)).fetchone()
                if owned:
                    c.execute('DELETE FROM reminders WHERE id=? AND user_id=?',(reminder,actor))
                    reminder_ids.append(reminder)
            c.execute('DELETE FROM expansion_habit_entries WHERE goal_id=? AND user_id=?',(goal_id,actor))
            c.execute('DELETE FROM expansion_point_events WHERE user_id=? AND (event_key LIKE ? OR event_key LIKE ?)',
                      (actor,f'habit:{goal_id}:%',f'reversal:habit:{goal_id}:%'))
            c.execute('DELETE FROM expansion_goals WHERE id=? AND user_id=?',(goal_id,actor))
            self.health._add_audit_log(c,actor,'expansion.goal.deleted','expansion_goal',goal_id)
        return {'deleted':True,'goal_id':goal_id,'deleted_count':1,'habit_entries_deleted':len(entries),'reminder_ids':reminder_ids}

    def checkin_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("goal_id","day","note"),("goal_id",)); checked_day=day(payload.get("day",today())); note=text(payload.get("note",""),1000,True) or ""
        if checked_day>today(): fail("future_checkin")
        with self.database.transaction() as c:
            goal=self._get("expansion_goals",actor,payload["goal_id"])
            if goal["status"]!="active": fail("invalid_state")
            existing=c.execute("SELECT * FROM expansion_habit_entries WHERE user_id=? AND goal_id=? AND day=?",(actor,goal["id"],checked_day)).fetchone()
            awarded=0
            if existing is None:
                cursor=c.execute("INSERT INTO expansion_habit_entries(user_id,goal_id,day,note,created_at) VALUES (?,?,?,?,?)",(actor,goal["id"],checked_day,note,now()))
                existing=c.execute("SELECT * FROM expansion_habit_entries WHERE id=? AND user_id=?",(int(cursor.lastrowid),actor)).fetchone()
                if checked_day==today(): awarded=self._award_points(c,actor,f"habit:{goal['id']}:{checked_day}",10,"当日习惯打卡",daily_limit=30)
        return {"goal":self.get_goal(actor,{"goal_id":goal["id"]}),"entry":dict(existing),"points_awarded":awarded}

    def uncheckin_goal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("goal_id","day"),("goal_id","day")); checked_day=day(payload["day"])
        with self.database.transaction() as c:
            goal=self._get("expansion_goals",actor,payload["goal_id"])
            c.execute("DELETE FROM expansion_habit_entries WHERE user_id=? AND goal_id=? AND day=?",(actor,goal["id"],checked_day))
            self._reverse_points(c,actor,f"habit:{goal['id']}:{checked_day}","取消习惯打卡")
        return {"goal":self.get_goal(actor,{"goal_id":goal["id"]}),"removed":True}

    def _award_points(self,c,actor,event_key,amount,reason,daily_limit=None):
        if c.execute("SELECT id FROM expansion_point_events WHERE user_id=? AND event_key=?",(actor,event_key)).fetchone(): return 0
        if daily_limit:
            prefix=event_key.split(":")[0]+":%"
            rows=c.execute("SELECT delta,created_at FROM expansion_point_events WHERE user_id=? AND event_key LIKE ? AND delta>0",(actor,prefix)).fetchall()
            earned=sum(r["delta"] for r in rows if datetime.fromisoformat(r["created_at"].replace("Z","+00:00")).astimezone(BEIJING).date().isoformat()==today())
            if earned+amount>daily_limit: return 0
        c.execute("INSERT INTO expansion_point_events(user_id,delta,event_key,reason,created_at) VALUES (?,?,?,?,?) ON CONFLICT(user_id,event_key) DO NOTHING",(actor,amount,event_key,reason,now()))
        return amount

    def _reverse_points(self,c,actor,event_key,reason):
        row=c.execute("SELECT delta FROM expansion_point_events WHERE user_id=? AND event_key=?",(actor,event_key)).fetchone()
        if row: self._award_points(c,actor,"reversal:"+event_key,-row["delta"],reason)

    def list_points(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("limit","offset")); page=self._page("expansion_point_events",actor,payload)
        with self.database.connect() as c:
            for item in page["items"]:
                item["balance_after"]=c.execute("SELECT COALESCE(SUM(delta),0) AS n FROM expansion_point_events WHERE user_id=? AND id<=?",(actor,item["id"])).fetchone()["n"]
        return page

    def get_growth(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,())
        with self.database.connect() as c:
            rows=c.execute("SELECT delta FROM expansion_point_events WHERE user_id=?",(actor,)).fetchall()
            days={r["day"] for r in c.execute("SELECT DISTINCT day FROM expansion_habit_entries WHERE user_id=?",(actor,)).fetchall()}
        balance=sum(r["delta"] for r in rows); streak=0; target=date.fromisoformat(today())
        if target.isoformat() not in days: target-=timedelta(days=1)
        while target.isoformat() in days: streak+=1; target-=timedelta(days=1)
        names=("青芽","新叶","繁枝","成荫","丰林","长青"); ranks=("I","II","III","IV","V")
        level=min(30,1+max(0,balance)//100); index=level-1; next_threshold=level*100 if level<30 else None
        level_info={"name":names[index//5],"tier":ranks[index%5],"next_name":(names[level//5]+" "+ranks[level%5]) if level<30 else None,"remaining":max(0,next_threshold-balance) if next_threshold is not None else 0,"next_threshold":next_threshold,"notice":"成长参与等级，不代表健康程度；每100积分一小级，共六段各五级"}
        return {"balance":balance,"earned":sum(max(0,r["delta"]) for r in rows),"spent":-sum(min(0,r["delta"]) for r in rows),"level":level,"level_info":level_info,"streak_days":streak,"rules":POINT_RULES,"invite_code":self.get_invite_code(actor,{})["code"]}

    def get_invite_code(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,())
        with self.database.connect() as c:
            row=c.execute("SELECT code FROM expansion_invitation_codes WHERE user_id=?",(actor,)).fetchone()
            counts=c.execute("SELECT status,COUNT(*) AS n FROM expansion_referrals WHERE inviter_user_id=? GROUP BY status",(actor,)).fetchall()
        return {"code":row["code"] if row else None,"accepted_count":sum(r["n"] for r in counts),"qualified_count":sum(r["n"] for r in counts if r["status"]=="qualified")}

    def create_invite_code(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,())
        with self.database.transaction() as c:
            c.execute("INSERT INTO expansion_invitation_codes(user_id,code,created_at) VALUES (?,?,?) ON CONFLICT(user_id) DO NOTHING",(actor,secrets.token_urlsafe(15),now()))
        return self.get_invite_code(actor,{})

    def accept_invite(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("code",),("code",)); code=text(payload["code"],100)
        with self.database.transaction() as c:
            row=c.execute("SELECT user_id FROM expansion_invitation_codes WHERE code=?",(code,)).fetchone()
            if row is None: fail("invalid_invitation")
            if row["user_id"]==actor: fail("self_invitation")
            if c.execute("SELECT id FROM expansion_referrals WHERE invitee_user_id=?",(actor,)).fetchone(): fail("invitation_already_bound")
            c.execute("INSERT INTO expansion_referrals(inviter_user_id,invitee_user_id,code,status,created_at) VALUES (?,?,?,'accepted',?)",(row["user_id"],actor,code,now()))
        return {"status":"accepted","inviter_display":"邀请会员","qualified":False}

    def _qualify_invite(self,c,actor):
        row=c.execute("SELECT id,inviter_user_id FROM expansion_referrals WHERE invitee_user_id=? AND status='accepted'",(actor,)).fetchone()
        if row:
            c.execute("UPDATE expansion_referrals SET status='qualified',qualified_at=? WHERE id=? AND invitee_user_id=? AND status='accepted'",(now(),row["id"],actor))
            # No reading another member's balance. The RLS insert exception is tied to this exact qualified referral.
            c.execute("INSERT INTO expansion_point_events(user_id,delta,event_key,reason,created_at) VALUES (?,20,?,'邀请会员完成首次确认',?) ON CONFLICT(user_id,event_key) DO NOTHING",(row["inviter_user_id"],"invite:"+str(row["id"]),now()))
