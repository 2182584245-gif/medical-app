from __future__ import annotations
import json
from .common import *

DEFAULT_SETTINGS={"onboarding_completed":False,"memory_enabled":False,"review_context_enabled":False,"food_preferences":[],"avoid_foods":[]}

class Records:
    def get_settings(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,())
        with self.database.connect() as c: row=c.execute("SELECT payload_json FROM expansion_settings WHERE user_id=?",(actor,)).fetchone()
        value={**DEFAULT_SETTINGS,**(json.loads(row["payload_json"]) if row else {})}
        return {**value,"version":version(value)}

    def update_settings(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("base_version","patch"),("base_version","patch"))
        patch=body(payload["patch"],DEFAULT_SETTINGS)
        with self.database.transaction() as c:
            current=self.get_settings(actor,{})
            match(current,payload["base_version"])
            for key,value in patch.items():
                if key.endswith("enabled") or key=="onboarding_completed": boolean(value)
                elif type(value) is not list or len(value)>30: fail()
                else:
                    for item in value: text(item,100)
            current.pop("version"); current.update(patch)
            c.execute("INSERT INTO expansion_settings(user_id,payload_json,updated_at) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",(actor,dumps(current),now()))
        return self.get_settings(actor,{})

    def _record(self,actor,record_id):
        with self.database.connect() as c: row=c.execute("SELECT * FROM life_records WHERE id=? AND user_id=?",(integer(record_id),actor)).fetchone()
        if row is None: fail("not_found")
        value=dict(row); value["details"]=json.loads(value.pop("details_json") or "null")
        with self.database.connect() as c: linked=c.execute("SELECT id FROM expansion_meals WHERE life_record_id=? AND user_id=?",(value["id"],actor)).fetchone()
        value['linked_meal_id']=linked['id'] if linked else None
        value["version"]=version(value)
        return value

    def get_record(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("record_id",),("record_id",))
        return self._record(actor,payload["record_id"])

    def list_records(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("category","limit","offset")); limit,offset=pagination(payload)
        args=[actor]; query=" FROM life_records WHERE user_id=?"
        if payload.get("category") is not None:
            query+=" AND category=?"; args.append(self.health._validate_category(payload["category"]))
        with self.database.connect() as c:
            total=c.execute("SELECT COUNT(*) AS n"+query,args).fetchone()["n"]
            rows=c.execute("SELECT id"+query+" ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
        return {"items":[self._record(actor,row["id"]) for row in rows],"total":total,"limit":limit,"offset":offset}

    def _update_record(self,c,actor,current,changes,reason):
        body(changes,("category","occurred_at","content","details"))
        merged={**current,**changes}
        category=self.health._validate_category(merged["category"])
        occurred,local,offset=self.health._normalize_local_timestamp(merged["occurred_at"])
        content=text(merged["content"],10000)
        details=self.health._validate_record_details(category,merged["details"])
        from ollama_chat_app.services.record_photos11839 import validate_owned_photos
        validate_owned_photos(c,actor,details)
        c.execute("UPDATE life_records SET category=?,occurred_at=?,local_date=?,timezone_offset_minutes=?,content=?,details_json=?,updated_at=? WHERE id=? AND user_id=?",(category,occurred,local,offset,content,None if details is None else dumps(details),now(),current["id"],actor))
        after=self._record(actor,current["id"])
        self._revision(c,actor,"life_record",current["id"],"update",current,after,reason)
        return after

    def update_record(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("record_id","base_version","changes","reason"),("record_id","base_version","changes","reason")); text(payload["reason"],500)
        with self.database.transaction() as c:
            current=self._record(actor,payload["record_id"]); match(current,payload["base_version"])
            if c.execute("SELECT id FROM expansion_meals WHERE life_record_id=? AND user_id=?",(current["id"],actor)).fetchone(): fail("linked_meal_requires_meal_edit")
            return self._update_record(c,actor,current,payload["changes"],payload["reason"])

    def delete_record(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("record_id","base_version","reason"),("record_id","base_version","reason")); reason=text(payload["reason"],500)
        with self.database.transaction() as c:
            current=self._record(actor,payload["record_id"]); match(current,payload["base_version"])
            meal=c.execute("SELECT id FROM expansion_meals WHERE life_record_id=? AND user_id=?",(current["id"],actor)).fetchone()
            if meal: self._reverse_points(c,actor,"meal:"+str(meal["id"]),"删除餐食记录")
            self._revision(c,actor,"life_record",current["id"],"delete",current,None,reason)
            c.execute("DELETE FROM life_records WHERE id=? AND user_id=?",(current["id"],actor))
        return {"deleted":True,"record_id":current["id"]}

    def list_record_revisions(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("record_id","limit","offset"),("record_id",)); integer(payload["record_id"])
        page=self._page("expansion_record_revisions",actor,payload,{"entity_type":"life_record","entity_id":payload["record_id"]})
        for row in page["items"]:
            row["before"]=json.loads(row.pop("before_json") or "null"); row["after"]=json.loads(row.pop("after_json") or "null")
        return page
