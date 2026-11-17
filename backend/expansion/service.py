"""Fixed actor-bound expansion RPC. Provider inference is deliberately outside this service."""
from __future__ import annotations
from datetime import date,timedelta
from pathlib import Path
from .common import *
from .contracts import ACTION_METHODS,NEW_TABLES,REVISION
from .records import Records
from .foods import Foods
from .member import Member
from .content import Content
from .analyses import Analyses

class ExpansionService(Records,Foods,Member,Content,Analyses,Base):
    def __init__(self,database,*,health=None,files=None,catalog_root=None):
        from ollama_chat_app.services.health import HealthService
        from ollama_chat_app.services.file_management import FileManagementService
        from ollama_chat_app.data.database import Database
        if isinstance(database,Database):
            from .sqlite_testing import AtomicSqliteDatabase
            database=AtomicSqliteDatabase(database)
        self.database=database
        # Shared transaction context is essential when reusing the legacy services.
        if health is not None and health.database is not database:
            raise ValueError("legacy_service_must_share_transaction_database")
        if files is not None and files.database is not database:
            raise ValueError("legacy_service_must_share_transaction_database")
        self.health=health or HealthService(database)
        self.files=files or FileManagementService(database)
        self.catalog_root=Path(catalog_root) if catalog_root else Path(__file__).resolve().parents[1]/"data"/"content-catalog"

    def _own_file(self,actor,file_id):
        with self.database.connect() as c: row=c.execute("SELECT id FROM user_files WHERE id=? AND user_id=?",(integer(file_id),actor)).fetchone()
        if row is None: fail("not_found")

    def _dated_page(self,table,actor,payload,datefield,filters=None):
        limit,offset=pagination(payload); query=f" FROM {table} WHERE user_id=?"; args=[actor]
        for key,value in (filters or {}).items():
            if value is not None: query+=f" AND {key}=?"; args.append(value)
        if payload.get("start_date") is not None:
            query+=f" AND {datefield}>=?"; args.append(iso(day(payload["start_date"])+"T00:00:00+08:00"))
        if payload.get("end_date") is not None:
            query+=f" AND {datefield}<?"; args.append(iso((date.fromisoformat(day(payload["end_date"]))+timedelta(days=1)).isoformat()+"T00:00:00+08:00"))
        if payload.get("start_date") and payload.get("end_date") and payload["start_date"]>payload["end_date"]: fail()
        with self.database.connect() as c:
            total=c.execute("SELECT COUNT(*) AS n"+query,args).fetchone()["n"]
            rows=c.execute("SELECT *"+query+f" ORDER BY {datefield} DESC,id DESC LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
        return {"items":[entity(r) for r in rows],"total":total,"limit":limit,"offset":offset}

    def _validate_refs(self,actor,refs):
        for reference in refs:
            if type(reference) is not str or ":" not in reference: fail("invalid_source_reference")
            kind,item_id=reference.split(":",1)
            if kind=="content": self.get_content(actor,{"content_id":item_id})
            else:
                if not item_id.isdecimal(): fail("invalid_source_reference")
                if kind=="life_record": self._record(actor,int(item_id))
                elif kind=="file": self._own_file(actor,int(item_id))
                elif kind in {"meal","observation","goal","memory"}: self._get({"meal":"expansion_meals","observation":"expansion_observations","goal":"expansion_goals","memory":"expansion_memories"}[kind],actor,int(item_id))
                else: fail("invalid_source_reference")

    def get_bootstrap(self,actor_user_id,payload):
        actor=self._actor(actor_user_id); body(payload,())
        return {"capabilities":{"revision":REVISION,"actions":sorted(set(ACTION_METHODS)-{"expansion.analyses.finish"}),"offline_mutations":False,"provider_inference":"client_deepseek_coordinator","exercise_sleep_expansion":False},"settings":self.get_settings(actor,{}),"summary":self.get_growth(actor,{})}

    def list_ai_scenes(self,actor_user_id,payload):
        self._actor(actor_user_id); body(payload,())
        return {"items":[{"id":key,"title":title,"description":description,"requires_context":False} for key,title,description in (("everyday","日常养生","基于本人描述提供一般健康教育"),("vegetarian","素食营养","讨论食物搭配与营养资料，不默认读取档案"),("weight","体重管理","观察本人选择提供的事实，不代替专业诊疗"),("glucose","血糖关注","保留单位与来源，不生成药物或治疗决策"))]}
