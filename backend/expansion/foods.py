from __future__ import annotations
import json
from uuid import uuid4
from .common import *
from .contracts import NUTRIENTS

MEAL_TYPES={"breakfast","lunch","dinner","snack"}

class Foods:
    def _food(self,actor,food_id):
        food_id=text(food_id,100)
        with self.database.connect() as c: row=c.execute("SELECT * FROM expansion_foods WHERE id=? AND (owner_user_id IS NULL OR owner_user_id=?)",(food_id,actor)).fetchone()
        if row is None: fail("not_found")
        value=entity(row)
        value["custom"]=value.get("owner_user_id") is not None
        with self.database.connect() as c: value["favorite"]=c.execute("SELECT 1 FROM expansion_food_favorites WHERE user_id=? AND food_id=?",(actor,food_id)).fetchone() is not None
        return value

    def get_food(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("food_id",),("food_id",))
        return self._food(actor,payload["food_id"])

    def list_foods(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("query","category","favorites_only","limit","offset")); limit,offset=pagination(payload)
        query=" FROM expansion_foods f WHERE (f.owner_user_id IS NULL OR f.owner_user_id=?)"; args=[actor]
        if "query" in payload: text(payload["query"],200,True)
        if "category" in payload: text(payload["category"],100,True)
        if payload.get("query"):
            query+=" AND LOWER(f.name) LIKE ? ESCAPE '!'"; term=text(payload["query"],200).lower().replace("!","!!").replace("%","!%").replace("_","!_"); args.append("%"+term+"%")
        if payload.get("category"):
            query+=" AND f.category=?"; args.append(text(payload["category"],100))
        if boolean(payload.get("favorites_only",False)):
            query+=" AND EXISTS (SELECT 1 FROM expansion_food_favorites v WHERE v.food_id=f.id AND v.user_id=?)"; args.append(actor)
        with self.database.connect() as c:
            total=c.execute("SELECT COUNT(*) AS n"+query,args).fetchone()["n"]
            rows=c.execute("SELECT f.id"+query+" ORDER BY f.name,f.id LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
        return {"items":[self._food(actor,r["id"]) for r in rows],"total":total,"limit":limit,"offset":offset}

    def list_food_favorites(self,actor_user_id,payload):
        body(payload,("limit","offset"))
        return self.list_foods(actor_user_id,{**payload,"favorites_only":True})

    def create_food(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("name","category","nutrients","serving_grams"),("name","nutrients"))
        name=text(payload["name"],200); category=text(payload.get("category","custom"),100)
        raw=body(payload["nutrients"],NUTRIENTS)
        nutrients={k:number(raw.get(k),0,100 if k=="gi" else 100000,True) for k in NUTRIENTS}
        # Self-entered GI is visible as self-report, never treated as verified GL basis.
        data={"nutrients":nutrients,"serving_grams":number(payload.get("serving_grams"),0.1,20000,True),"source":{"name":"会员本人录入","status":"self_report","basis":"per_100g"}}
        food_id="custom:"+uuid4().hex; stamp=now()
        with self.database.transaction() as c:
            c.execute("INSERT INTO expansion_foods(id,owner_user_id,name,category,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(food_id,actor,name,category,dumps(data),stamp,stamp))
        return self._food(actor,food_id)

    def set_food_favorite(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("food_id","favorite"),("food_id","favorite")); favorite=boolean(payload["favorite"]); food=self._food(actor,payload["food_id"])
        with self.database.transaction() as c:
            if favorite: c.execute("INSERT INTO expansion_food_favorites(user_id,food_id,created_at) VALUES (?,?,?) ON CONFLICT(user_id,food_id) DO NOTHING",(actor,food["id"],now()))
            else: c.execute("DELETE FROM expansion_food_favorites WHERE user_id=? AND food_id=?",(actor,food["id"]))
        return {"food_id":food["id"],"favorite":favorite}

    def _meal_items(self,actor,raw):
        if type(raw) is not list or not 1<=len(raw)<=20: fail()
        result=[]
        for item in raw:
            body(item,("food_id","name","amount_g"),("name","amount_g")); name=text(item["name"],200); amount=number(item["amount_g"],0.1,20000,True)
            food=self._food(actor,item["food_id"]) if item.get("food_id") else None
            nutrients={key:None for key in NUTRIENTS}
            if food and amount is not None:
                nutrients={key:None if food["nutrients"].get(key) is None else round(food["nutrients"][key]*(1 if key=="gi" else amount/100),4) for key in NUTRIENTS}
            source=food["source"] if food else {"name":"本人核对食物","status":"unmatched"}
            gl=None
            if food and amount is not None and source.get("gi_source") and source.get("available_carbohydrate_source") and nutrients["gi"] is not None and nutrients["available_carbohydrate_g"] is not None:
                gl=round(nutrients["gi"]*nutrients["available_carbohydrate_g"]/100,2)
            result.append({"food_id":food["id"] if food else None,"name":name,"amount_g":amount,"nutrients":nutrients,"source":source,"glycemic_load":gl})
        return result

    def _meal_data(self,actor,payload,current=None):
        merged={**(current or {}),**payload}; meal_type=choice(merged["meal_type"],MEAL_TYPES); occurred=iso(merged["occurred_at"])
        if "items" in payload:
            raw=payload["items"]
            same=current is not None and type(raw) is list and all(type(i) is dict for i in raw) and [{k:i.get(k) for k in ("food_id","name","amount_g")} for i in raw]==[{k:i.get(k) for k in ("food_id","name","amount_g")} for i in current["items"]]
            if same:
                for item in raw:
                    body(item,("food_id","name","amount_g"),("name","amount_g")); text(item["name"],200); number(item["amount_g"],0.1,20000,True)
                    if item.get("food_id") is not None: text(item["food_id"],100)
                items=current["items"]
            else: items=self._meal_items(actor,raw)
        else: items=current["items"]
        totals={k:(None if any(i["nutrients"][k] is None for i in items) else round(sum(i["nutrients"][k] for i in items),4)) for k in NUTRIENTS if k!="gi"}
        data={"items":items,"notes":text(merged.get("notes",""),4000,True) or "","totals":totals,"photo_file_id":merged.get("photo_file_id")}
        if data["photo_file_id"] is not None: self._own_file(actor,data["photo_file_id"])
        details={}
        if all(i["amount_g"] is not None for i in items): details["amount_g"]=sum(i["amount_g"] for i in items)
        if totals["energy_kcal"] is not None: details["calories_kcal"]=round(totals["energy_kcal"])
        content="、".join(i["name"] for i in items)+(f"；{data['notes']}" if data["notes"] else "")
        return meal_type,occurred,data,details,content

    def create_meal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("meal_type","occurred_at","items","notes","photo_file_id"),("meal_type","occurred_at","items"))
        return self._create_meal(actor,payload,"manual")

    def _create_meal(self,actor,payload,source):
        with self.database.transaction() as c:
            kind,occurred,data,details,content=self._meal_data(actor,payload)
            record_id=self.health.add_life_record(actor,"diet",occurred,content,source=source,details=details)
            meal=self._insert("expansion_meals",actor,{"life_record_id":record_id,"occurred_at":occurred,"meal_type":kind},data)
            self._revision(c,actor,"life_record",record_id,"create",None,self._record(actor,record_id),"本人确认餐食")
            self._award_points(c,actor,"meal:"+str(meal["id"]),2,"餐食记录",daily_limit=8)
            self._qualify_invite(c,actor)
        return meal

    def get_meal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("meal_id",),("meal_id",)); return self._get("expansion_meals",actor,payload["meal_id"])

    def list_meals(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("limit","offset","start_date","end_date")); return self._dated_page("expansion_meals",actor,payload,"occurred_at")

    def update_meal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("meal_id","base_version","meal_type","occurred_at","items","notes"),("meal_id","base_version"))
        with self.database.transaction() as c:
            current=self._get("expansion_meals",actor,payload["meal_id"]); match(current,payload["base_version"])
            kind,occurred,data,details,content=self._meal_data(actor,payload,current)
            self._update_record(c,actor,self._record(actor,current["life_record_id"]),{"category":"diet","occurred_at":occurred,"content":content,"details":details},"本人修改餐食")
            return self._replace("expansion_meals",actor,current,{"occurred_at":occurred,"meal_type":kind},data)

    def delete_meal(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("meal_id","base_version","reason"),("meal_id","base_version","reason"))
        with self.database.transaction() as c:
            current=self._get("expansion_meals",actor,payload["meal_id"]); match(current,payload["base_version"])
            record=self._record(actor,current["life_record_id"])
            self.delete_record(actor,{"record_id":record["id"],"base_version":record["version"],"reason":payload["reason"]})
        return {"deleted":True,"meal_id":current["id"]}
