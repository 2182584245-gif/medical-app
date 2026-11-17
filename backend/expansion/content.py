from __future__ import annotations
import json
from pathlib import Path
from .common import *

class Content:
    def _catalog(self):
        try:
            data=json.loads((self.catalog_root/"catalog.json").read_text(encoding="utf-8"))
            curated=json.loads((self.catalog_root/"daily-readings.json").read_text(encoding="utf-8"))
        except (OSError,ValueError): fail("content_unavailable")
        if data.get("schema_version")!=1 or curated.get("schema_version")!=1: fail("content_unavailable")
        documents=data["documents"]
        supplemental=self.catalog_root/"supplemental-content.json"
        if supplemental.exists():
            try: extra=json.loads(supplemental.read_text(encoding="utf-8"))
            except (OSError,ValueError): fail("content_unavailable")
            if extra.get("schema_version")!=1 or type(extra.get("documents")) is not list: fail("content_unavailable")
            documents=[*documents,*extra["documents"]]
        if len({i["id"] for i in documents})!=len(documents): fail("content_unavailable")
        return documents,curated["items"]

    @staticmethod
    def _public_content(item):
        keys=("id","document_id","title","category","tags","summary","source","source_status","author_text","publication_text","author_contact_notice","medical_review","claim_notice","provenance_notice","review_notes","daily_reading","reading_minutes","original_document_id","original_paragraph_ids","servings","nutrition_per_serving","solar_longitude_degrees","exact_date")
        return {"reading_minutes":None,**{k:item[k] for k in keys if k in item}}

    def list_content(self,actor_user_id,payload):
        self._actor(actor_user_id); body(payload,("query","category","limit","offset")); limit,offset=pagination(payload)
        documents,curated=self._catalog(); daily={i["id"] for i in documents if i.get("original_path")}; category=payload.get("category")
        if category is not None: choice(category,{"internal_article","daily","daily_reading","recipe","seasonal","topic"})
        query=(text(payload.get("query",""),200,True) or "").casefold()
        def matches(item):
            if category in {"daily","daily_reading"}: return item["id"] in daily
            if category is None: return True
            return item.get("category")==category or category in item.get("tags",[])
        items=[self._public_content(i) for i in documents if matches(i) and (not query or query in (i.get("title","")+" "+i.get("summary","")).casefold())]
        return {"items":items[offset:offset+limit],"total":len(items),"limit":limit,"offset":offset,"categories":[{"id":key,"label":label} for key,label in (("internal_article","资料原文"),("daily_reading","每日精选"),("recipe","食谱专题"),("seasonal","节气阅读"),("topic","营养专题"))]}

    def get_content(self,actor_user_id,payload):
        self._actor(actor_user_id); body(payload,("content_id",),("content_id",)); content_id=text(payload["content_id"],100)
        documents,curated=self._catalog(); item=next((i for i in documents if i["id"]==content_id),None)
        if item is None: fail("not_found")
        if "body" in item:
            return {**self._public_content(item),"body":text(item["body"],100000),"blocks":[{"id":"B1","tag":"p","text":item["body"],"id_origin":"generated_text_block"}],"daily_excerpt":None}
        relative=item.get("original_path")
        if type(relative) is not str: fail("content_unavailable")
        path=(self.catalog_root/relative).resolve()
        if not path.is_relative_to(self.catalog_root.resolve()) or path.is_symlink(): fail("content_unavailable")
        try: original=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): fail("content_unavailable")
        blocks=original.get("blocks",[])
        if type(blocks) is not list or not all(type(b) is dict and type(b.get("text")) is str for b in blocks): fail("content_unavailable")
        result={**self._public_content(item),"blocks":[{k:b[k] for k in ("id","tag","text","id_origin") if k in b} for b in blocks],"body":"\n\n".join(b["text"] for b in blocks)}
        result["daily_excerpt"]=next((i.get("excerpt") for i in curated if i.get("document_id",i.get("id"))==content_id),None)
        return result

    def get_content_progress(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("content_id",),("content_id",)); self.get_content(actor,payload)
        with self.database.connect() as c: row=c.execute("SELECT * FROM expansion_content_progress WHERE user_id=? AND content_id=?",(actor,payload["content_id"])).fetchone()
        if row is None: return {"content_id":payload["content_id"],"progress":0,"saved":False,"completed_at":None,"updated_at":None}
        value=dict(row); value["saved"]=bool(value["saved"]); return value

    def update_content_progress(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,("content_id","progress","saved"),("content_id","progress")); progress=number(payload["progress"],0,1)
        self.get_content(actor,{"content_id":payload["content_id"]})
        with self.database.transaction() as c:
            current=self.get_content_progress(actor,{"content_id":payload["content_id"]}); saved=boolean(payload.get("saved",current["saved"])); completed=current["completed_at"] or (now() if progress==1 else None)
            c.execute("INSERT INTO expansion_content_progress(user_id,content_id,progress,saved,completed_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,content_id) DO UPDATE SET progress=excluded.progress,saved=excluded.saved,completed_at=excluded.completed_at,updated_at=excluded.updated_at",(actor,payload["content_id"],progress,int(saved),completed,now()))
            if progress==1: self._award_points(c,actor,"reading:"+payload["content_id"],3,"首次完成文章阅读",daily_limit=9)
        return self.get_content_progress(actor,{"content_id":payload["content_id"]})
