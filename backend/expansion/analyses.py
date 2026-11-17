from __future__ import annotations
import copy,json
from datetime import date,timedelta
from .common import *
from .contracts import ANALYSIS_KINDS, REVIEW_KINDS
MAX_CONTEXT_REFS=501
REVIEW_CONTEXT_CHAR_LIMIT=12000

class Analyses:
    def create_analysis(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("kind","source_file_id","source_text","scope"),("kind",)); kind=choice(payload["kind"],ANALYSIS_KINDS)
        file_id=payload.get("source_file_id"); source_text=text(payload.get("source_text",""),20000,True) or ""
        if file_id is not None: self._own_file(actor,file_id)
        if kind in {"meal_photo","report_prefill"} and file_id is None: fail("source_file_required")
        if kind=="memory_extract" and not source_text: fail("source_text_required")
        scope=body(payload.get("scope",{}),("meal_type","occurred_at") if kind=="meal_photo" else ("meal_id","start_date","end_date"))
        if kind=="meal_photo":
            if "meal_type" in scope: choice(scope["meal_type"],{"breakfast","lunch","dinner","snack"})
            if "occurred_at" in scope: scope={**scope,"occurred_at":iso(scope["occurred_at"])}
        if "meal_id" in scope: self._get("expansion_meals",actor,scope["meal_id"])
        if kind=="meal_review" and "meal_id" not in scope: fail("meal_required")
        for key in ("start_date","end_date"):
            if key in scope: day(scope[key])
        if "start_date" in scope and "end_date" in scope and scope["start_date"]>scope["end_date"]: fail()
        return self._insert("expansion_analyses",actor,{"kind":kind,"status":"pending"},{"source_file_id":file_id,"source_text":source_text,"scope":scope,"result":None,"model":None,"created_entities":[],"prepared_source_refs":[]})

    def get_analysis(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("analysis_id",),("analysis_id",)); return self._get("expansion_analyses",actor,payload["analysis_id"])

    def list_analyses(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("kind","limit","offset")); kind=payload.get("kind")
        if kind is not None: choice(kind,ANALYSIS_KINDS)
        return self._page("expansion_analyses",actor,payload,{"kind":kind})

    def list_reviews(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("limit","offset")); limit,offset=pagination(payload)
        kinds=sorted(REVIEW_KINDS); marks=','.join('?' for _ in kinds)
        with self.database.connect() as c:
            total=c.execute(f"SELECT COUNT(*) AS n FROM expansion_analyses WHERE user_id=? AND kind IN ({marks})",(actor,*kinds)).fetchone()["n"]
            rows=c.execute(f"SELECT * FROM expansion_analyses WHERE user_id=? AND kind IN ({marks}) ORDER BY id DESC LIMIT ? OFFSET ?",(actor,*kinds,limit,offset)).fetchall()
        return {"items":[entity(r) for r in rows],"total":total,"limit":limit,"offset":offset}

    def _memory_context(self,actor,include):
        if not include or not self.get_settings(actor,{})["memory_enabled"]: return []
        return self.list_memories(actor,{"status":"confirmed","limit":100})["items"]

    def get_review_context(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("start_date","end_date","consent","include_memory","meal_id"),("start_date","end_date","consent"))
        start=day(payload["start_date"]); end=day(payload["end_date"])
        if start>end or (date.fromisoformat(end)-date.fromisoformat(start)).days>92: fail("range_too_large")
        if not boolean(payload["consent"]) or not self.get_settings(actor,{})["review_context_enabled"]: fail("consent_required")
        datepayload={"start_date":start,"end_date":end,"limit":100}
        record_page=self._dated_page("life_records",actor,datepayload,"occurred_at"); records=record_page["items"]
        for record in records: record["details"]=__import__('json').loads(record.pop("details_json") or "null")
        meal_page=self.list_meals(actor,datepayload); meals=meal_page["items"]
        if payload.get("meal_id") is not None:
            meals=[self._get("expansion_meals",actor,payload["meal_id"])]; meal_page={"total":1}
            records=[self._record(actor,meals[0]["life_record_id"])]; record_page={"total":1}
        observation_page=self.list_observations(actor,datepayload); observations=observation_page["items"]
        goal_page=self._page("expansion_goals",actor,{"limit":100},{"status":"active"}); goals=goal_page["items"]
        if payload.get("meal_id") is not None:
            observations=[]; observation_page={"total":0}; goals=[]; goal_page={"total":0}
            meal_day=datetime.fromisoformat(meals[0]["occurred_at"].replace('Z','+00:00')).astimezone(BEIJING).date().isoformat()
            start=end=meal_day
        memories=self._memory_context(actor,boolean(payload.get("include_memory",False)))
        with self.database.connect() as c:
            memory_total=c.execute("SELECT COUNT(*) AS n FROM expansion_memories WHERE user_id=? AND status='confirmed'",(actor,)).fetchone()["n"] if boolean(payload.get("include_memory",False)) and self.get_settings(actor,{})["memory_enabled"] else 0
        totals={"records":record_page["total"],"meals":meal_page["total"],"observations":observation_page["total"],"goals":goal_page["total"],"memories":memory_total}
        fields={"records":("id","category","occurred_at","content","details","source"),"meals":("id","life_record_id","occurred_at","meal_type","items","totals","notes"),"observations":("id","kind","measured_at","value","unit","context","notes","report_item_id"),"goals":("id","title","kind","target","unit","frequency"),"memories":("id","key","value","kind","source_ref")}
        candidates={key:[{field:item[field] for field in fields[key] if field in item} for item in values] for key,values in (("records",records),("meals",meals),("observations",observations),("goals",goals),("memories",memories))}
        result={"period":{"start_date":start,"end_date":end},**{key:[] for key in candidates},"source_refs":[],"generated_at":now()}
        prefix={"records":"life_record","meals":"meal","observations":"observation","goals":"goal","memories":"memory"}
        # Whole fact objects only. Explicit coverage reports any omitted rows;
        # never cut a measurement/value in half or imply a complete monthly ledger.
        for index in range(100):
            for key,items in candidates.items():
                if index>=len(items): continue
                item=items[index]; result[key].append(item); result["source_refs"].append(prefix[key]+":"+str(item["id"]))
                if len(json.dumps(result,ensure_ascii=False,separators=(",",":")))>REVIEW_CONTEXT_CHAR_LIMIT-2000:
                    result[key].pop(); result["source_refs"].pop()
        returned={key:len(result[key]) for key in candidates}
        result["coverage"]={**{key:returned[key] for key in ("records","meals","observations")},"totals":totals,"returned":returned,"bounded_limit_per_kind":100,"truncated":any(returned[key]<totals[key] for key in totals),"truncated_resources":[key for key in totals if returned[key]<totals[key]],"context_char_limit":REVIEW_CONTEXT_CHAR_LIMIT,"missing_data":[key for key in ("records","meals","observations") if not result[key]],"linked_meal_notice":"meal.life_record_id 与同编号饮食记录属于同一事实，请勿重复计算"}
        result["coverage"]["context_chars"]=len(dumps(result))
        return result

    def get_analysis_context(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("analysis_id","include_memory","consent"),("analysis_id",)); include=boolean(payload.get("include_memory",False)); consent=boolean(payload.get("consent",False))
        with self.database.transaction():
            current=self._get("expansion_analyses",actor,payload["analysis_id"])
            if current["status"]!="pending": fail("invalid_state")
            context={"source_text":current["source_text"],"source_file_id":current["source_file_id"]}; refs=[]
            if current["kind"]=="meal_photo": context["scope"]=current["scope"]
            if current["source_file_id"] is not None:
                self._own_file(actor,current["source_file_id"]); refs.append("file:"+str(current["source_file_id"]))
            if current["kind"] in REVIEW_KINDS and current["kind"]!="nutrition_check":
                scope=current["scope"]; end=scope.get("end_date",today()); count=30 if current["kind"]=="monthly_review" else 7 if current["kind"]=="weekly_review" else 1
                start=scope.get("start_date",(date.fromisoformat(end)-timedelta(days=count-1)).isoformat())
                context["review"]=self.get_review_context(actor,{"start_date":start,"end_date":end,"consent":consent,"include_memory":include,**({"meal_id":scope["meal_id"]} if scope.get("meal_id") else {})}); refs+=context["review"]["source_refs"]
            elif current["kind"]=="nutrition_check" and consent and self.get_settings(actor,{})["review_context_enabled"]:
                scope=current["scope"]; end=scope.get("end_date",today()); start=scope.get("start_date",end)
                context["review"]=self.get_review_context(actor,{"start_date":start,"end_date":end,"consent":True,"include_memory":include}); refs+=context["review"]["source_refs"]
            elif current["kind"]!="nutrition_check":
                memories=self._memory_context(actor,include)
                if memories:
                    context["memories"]=[]
                    for memory in memories:
                        fact={k:memory[k] for k in ("id","key","value","kind","source_ref")}
                        context["memories"].append(fact)
                        if len(dumps(context["memories"]))>6000:
                            context["memories"].pop(); continue
                        refs.append("memory:"+str(memory["id"]))
                    with self.database.connect() as c: total_memories=c.execute("SELECT COUNT(*) AS n FROM expansion_memories WHERE user_id=? AND status='confirmed'",(actor,)).fetchone()["n"]
                    context["memory_coverage"]={"returned":len(context["memories"]),"available_in_page":len(memories),"total":total_memories,"truncated":len(context["memories"])<total_memories}
            # Reserve room for provider system instructions, attachments and RAG.
            if len(context["source_text"])>6000:
                context["source_text_original_chars"]=len(context["source_text"])
                context["source_text"]=context["source_text"][:6000]
                context["source_text_truncated"]=True
            data=self._analysis_payload(current); data["prepared_source_refs"]=refs
            current=self._replace("expansion_analyses",actor,current,{},data)
        return {"analysis_id":current["id"],"kind":current["kind"],"context":context,"source_refs":refs,"generated_at":now(),"base_version":current["version"]}

    @staticmethod
    def _analysis_payload(value):
        return {k:value.get(k) for k in ("source_file_id","source_text","scope","result","model","created_entities","prepared_source_refs")}

    def _normal_result(self,actor,kind,result,refs):
        if kind=="meal_photo":
            body(result,("items","meal_type","occurred_at","notes"),("items","meal_type","occurred_at")); choice(result["meal_type"],{"breakfast","lunch","dinner","snack"}); iso(result["occurred_at"])
            # AI guesses remain proposals; nutrient snapshots are rebuilt only on human confirmation.
            self._meal_items(actor,result["items"]); text(result.get("notes",""),4000,True)
        elif kind=="report_prefill":
            body(result,("report_type","report_date","institution","items"),("report_type","items")); text(result["report_type"],100)
            if result.get("report_date") is not None: day(result["report_date"])
            text(result.get("institution"),500,True)
            if type(result["items"]) is not list or not 1<=len(result["items"])<=50: fail()
            for item in result["items"]:
                body(item,("item_name","result_value","unit","reference_range","flag","source_text","page"),("item_name","result_value")); text(item["item_name"],500); text(item["result_value"],1000,True)
                for key,maximum in (("unit",100),("reference_range",1000),("source_text",2000)): text(item.get(key),maximum,True)
                choice(item.get("flag","unknown"),{"unknown","normal","low","high"})
                if item.get("page") is not None: number(integer(item["page"]),1,10000)
        elif kind=="memory_extract":
            body(result,("items",),("items",))
            if type(result["items"]) is not list or not 1<=len(result["items"])<=20: fail()
            for item in result["items"]:
                body(item,("key","value","kind","source_ref"),("key","value")); self._memory_value(item); choice(item.get("kind","preference"),{"preference","routine","explicit_fact"})
                if item.get("source_ref") not in (None,*refs): fail("invalid_source_reference")
        else:
            body(result,("answer","source_refs","missing_data"),("answer","source_refs","missing_data")); text(result["answer"],16000)
            for key in ("source_refs","missing_data"):
                if type(result[key]) is not list or len(result[key])>(MAX_CONTEXT_REFS if key=="source_refs" else 100): fail()
                for item in result[key]: text(item,200)
            if not set(result["source_refs"])<=set(refs): fail("invalid_source_reference")
        dumps(result)
        return copy.deepcopy(result)

    def finish_analysis(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("analysis_id","base_version","result","model"),("analysis_id","base_version","result"))
        with self.database.transaction():
            current=self._get("expansion_analyses",actor,payload["analysis_id"]); match(current,payload["base_version"])
            if current["status"]!="pending": fail("invalid_state")
            result=self._normal_result(actor,current["kind"],payload["result"],current["prepared_source_refs"])
            data=self._analysis_payload(current); data.update(result=result,model=text(payload.get("model"),100,True))
            return self._replace("expansion_analyses",actor,current,{"status":"ready"},data)

    def confirm_analysis(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("analysis_id","base_version","selected_indices","overrides","additional_items"),("analysis_id","base_version"))
        with self.database.transaction():
            current=self._get("expansion_analyses",actor,payload["analysis_id"]); match(current,payload["base_version"])
            if current["status"]!="ready": fail("invalid_state")
            result=copy.deepcopy(current["result"]); kind=current["kind"]; created=[]
            if kind in REVIEW_KINDS:
                if payload.get("selected_indices") or payload.get("overrides") or payload.get("additional_items"): fail()
            else:
                indices=payload.get("selected_indices",list(range(len(result["items"]))))
                additional=payload.get("additional_items",[])
                if type(additional) is not list or (additional and kind!="meal_photo"): fail()
                if type(indices) is not list or (not indices and not additional) or any(type(i) is not int or not 0<=i<len(result["items"]) for i in indices) or len(indices)!=len(set(indices)): fail()
                allowed=("items","meal_type","occurred_at","notes") if kind=="meal_photo" else ("items","report_date","report_type","institution") if kind=="report_prefill" else ("items",)
                overrides=body(payload.get("overrides",{}),allowed); item_overrides=body(overrides.get("items",{}),[str(i) for i in indices])
                result.update({k:v for k,v in overrides.items() if k!="items"})
                result["items"]=[{**result["items"][i],**body(item_overrides.get(str(i),{}),("food_id","name","amount_g") if kind=="meal_photo" else ("item_name","result_value","unit","reference_range","flag","source_text","page") if kind=="report_prefill" else ("key","value","kind","source_ref"))} for i in indices]
                result["items"].extend(additional)
                self._normal_result(actor,kind,result,current["prepared_source_refs"])
                if kind=="meal_photo":
                    meal=self._create_meal(actor,{**result,"photo_file_id":current["source_file_id"]},"ai_confirmed"); created.append({"kind":"meal","id":meal["id"]})
                elif kind=="report_prefill":
                    report_id=self.files.create_report_draft(actor,current["source_file_id"],report_type=result["report_type"],report_date=result.get("report_date"),institution=result.get("institution"))
                    created.append({"kind":"report","id":report_id})
                    for item in result["items"]:
                        item_id=self.files.add_report_item_draft(actor,report_id,item["item_name"],result_value=item.get("result_value"),unit=item.get("unit"),reference_range=item.get("reference_range"),flag=item.get("flag","unknown")); created.append({"kind":"report_item","id":item_id})
                else:
                    for item in result["items"]:
                        memory=self.create_memory(actor,item); memory=self.confirm_memory(actor,{"memory_id":memory["id"],"base_version":memory["version"]}); created.append({"kind":"memory","id":memory["id"]})
            data=self._analysis_payload(current); data.update(result=result,created_entities=created)
            analysis=self._replace("expansion_analyses",actor,current,{"status":"confirmed"},data)
        return {"analysis":analysis,"created":created}

    def dismiss_analysis(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("analysis_id","base_version"),("analysis_id","base_version"))
        with self.database.transaction():
            current=self._get("expansion_analyses",actor,payload["analysis_id"]); match(current,payload["base_version"])
            if current["status"] not in {"pending","ready"}: fail("invalid_state")
            return self._replace("expansion_analyses",actor,current,{"status":"dismissed"},self._analysis_payload(current))
