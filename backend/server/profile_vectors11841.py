"""Current-owner/current-version introduction retrieval using real pgvector.

Only the explicitly saved introduction enters the authorized encoder. Credentials,
IP addresses, usage metadata and other database columns never enter the model.
The encoder is cloud-only; there is no lexical-vector or raw-text AI fallback.
"""
from __future__ import annotations
from contextlib import contextmanager,nullcontext
from datetime import datetime, timezone
import hashlib,json,math,threading,time
from .profile_embedding11841 import ChineseProfileEmbedder,MODEL_REVISION,EmbeddingUnavailable

TABLE='member_profile_vectors11841'
EXT='medical_app_extensions'

def revision(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()
def stamp(value):return value.astimezone(timezone.utc).isoformat()
def parsed(value):
    try:
        result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result if result.tzinfo is not None else None
    except (TypeError,ValueError):return None
def vector_literal(values):
    if len(values)!=512 or any(type(v)not in(int,float)or not math.isfinite(v)for v in values):raise ValueError('Invalid semantic vector')
    return '['+','.join(format(float(v),'.9g')for v in values)+']'
def chunks(text):
    # Preserve the existing stable excerpts and provenance. The cloud encoder
    # receives independent chunks in a bounded batch, not a fused profile.
    return [text[n:n+400]for n in range(0,len(text),360)]

class ProfileVectorService:
    def __init__(self,database,*,embedder=None,clock=None):
        self.db=database;self.embedder=embedder or ChineseProfileEmbedder()
        self.clock=clock or(lambda:datetime.now(timezone.utc))
        self._lock=threading.Lock()
    @contextmanager
    def _serial(self):
        if not self._lock.acquire(timeout=5):
            raise EmbeddingUnavailable('Profile semantic retrieval is busy')
        try:yield
        finally:self._lock.release()
    def _send_guard(self,actor,version,controls):
        # This runs again after the provider's network lock, immediately before
        # transmitting. No database transaction remains open across HTTP.
        self._actor(actor)
        with self.db.connect()as conn:
            member=conn.execute('SELECT role_code,account_status FROM users WHERE id=?',(actor,)).fetchone()
            if not member or member['role_code']!='member'or member['account_status']!='active':
                raise PermissionError('Profile owner is no longer active')
            identity=self.db._identity.get()
            if getattr(identity,'token_digest',None):
                from .platform_database import require_live_write_session
                require_live_write_session(conn,identity)
            current=self._current(conn,actor)
            if not self._allowed(conn,actor,current,controls)or revision(current['personal_introduction'])!=version:
                raise EmbeddingUnavailable('Profile authorization or revision changed')
    def _embed_many(self,texts,*,query,guard,deadline):
        batch=getattr(self.embedder,'embed_many',None)
        if callable(batch):
            values=batch(texts,query=query,pre_send=guard,deadline=deadline)
        else:
            # Preserve the existing injectable encoder seam for isolated tests.
            # The production Qwen adapter always implements guarded batching.
            values=[]
            for text in texts:
                guard()
                values.append(self.embedder.embed(text,query=query))
        if not isinstance(values,(list,tuple))or len(values)!=len(texts):
            raise EmbeddingUnavailable('Profile embedding result count differs')
        for value in values:vector_literal(value)
        return values
    def _actor(self,actor):
        identity=self.db._identity.get()
        if type(actor)is not int or actor<=0 or identity.user_id!=actor or identity.role_code!='member':raise PermissionError('Profile owner differs')
        if getattr(self.db,'dialect','sqlite')!='postgresql':raise EmbeddingUnavailable('Semantic retrieval requires reviewed PostgreSQL vector extension')
    def _current(self,conn,actor):
        row=conn.execute('SELECT personal_introduction,personal_introduction_updated_at FROM member_profiles WHERE user_id=?',(actor,)).fetchone()
        return None if row is None else dict(row)
    def _allowed(self,conn,actor,row,controls):
        if controls and controls.get('profile')is False:return False
        pref=conn.execute('SELECT preferences_json FROM user_preferences WHERE user_id=?',(actor,)).fetchone()
        if pref:
            try:
                if json.loads(pref['preferences_json']).get('mobile_ai_personal_enabled',True)is not True:return False
            except (ValueError,TypeError,AttributeError):return False
        control=conn.execute("SELECT enabled_for_ai,collect_events,forget_before FROM member_memory_controls WHERE user_id=? AND domain='profile'",(actor,)).fetchone()
        if not row or not row['personal_introduction'].strip():return False
        if control:
            if not control['enabled_for_ai']or not control['collect_events']:return False
            cutoff=parsed(control['forget_before'])if control['forget_before']else None
            updated=parsed(row['personal_introduction_updated_at'])
            if control['forget_before']and(not cutoff or not updated or updated<=cutoff):return False
        return True
    def invalidate(self,actor,connection=None):
        """Revoke derived content only; never alter the saved introduction."""
        self._actor(actor)
        with (nullcontext(connection)if connection is not None else self.db.transaction())as conn:
            conn.execute('DELETE FROM '+TABLE+' WHERE user_id=?',(actor,))
    def retrieve(self,actor,question,*,controls=None):
        self._actor(actor)
        if type(question)is not str or not 1<=len(question)<=4000:raise ValueError('Invalid profile query')
        # Serialize model calls per process. Expensive embedding happens outside
        # the write transaction; a locked recheck prevents stale reinsertion.
        with self._serial():
            deadline=time.monotonic()+40
            with self.db.transaction()as conn:
                row=self._current(conn,actor)
                if not self._allowed(conn,actor,row,controls):
                    conn.execute('DELETE FROM '+TABLE+' WHERE user_id=?',(actor,));return []
                text=row['personal_introduction'];version=revision(text)
                conn.execute('DELETE FROM '+TABLE+' WHERE user_id=? AND (profile_revision<>? OR model_revision<>?)',(actor,version,MODEL_REVISION))
                count=conn.execute('SELECT COUNT(*) AS n FROM '+TABLE+' WHERE user_id=? AND profile_revision=? AND model_revision=?',(actor,version,MODEL_REVISION)).fetchone()['n']
            pieces=chunks(text)
            guard=lambda:self._send_guard(actor,version,controls)
            vectors=None if count==len(pieces)else self._embed_many(pieces,query=False,guard=guard,deadline=deadline)
            query=vector_literal(self._embed_many([question],query=True,guard=guard,deadline=deadline)[0])
            with self.db.transaction()as conn:
                # HealthService updates this same owner row. PostgreSQL locks it
                # until the query has completed, including against concurrent
                # save/forget; final stream settings checks cover later changes.
                current=conn.execute('SELECT personal_introduction,personal_introduction_updated_at FROM member_profiles WHERE user_id=? FOR UPDATE',(actor,)).fetchone()
                current=dict(current)if current else None
                if not self._allowed(conn,actor,current,controls)or revision(current['personal_introduction'])!=version:
                    conn.execute('DELETE FROM '+TABLE+' WHERE user_id=?',(actor,));return []
                if vectors is not None:
                    conn.execute('DELETE FROM '+TABLE+' WHERE user_id=?',(actor,))
                    at=stamp(self.clock())
                    for index,(piece,vector)in enumerate(zip(pieces,vectors)):
                        conn.execute('INSERT INTO '+TABLE+'(user_id,profile_revision,model_revision,chunk_no,content,content_hash,embedding,updated_at) '
                                     'VALUES(?,?,?,?,?,?,CAST(? AS '+EXT+'.vector),?)',
                            (actor,version,MODEL_REVISION,index,piece,revision(piece),vector_literal(vector),at))
                # This is exact cosine distance evaluated by PostgreSQL's
                # installed pgvector operator, over <=12 owner profile chunks.
                hits=conn.execute('SELECT content,content_hash,chunk_no,embedding OPERATOR('+EXT+'.<=>) CAST(? AS '+EXT+'.vector) AS distance '
                    'FROM '+TABLE+' WHERE user_id=? AND profile_revision=? AND model_revision=? ORDER BY distance,chunk_no LIMIT 3',
                    (query,actor,version,MODEL_REVISION)).fetchall()
                return [{'text':r['content'],'source':'user_saved_personal_introduction','profile_revision':version,
                         'model_revision':MODEL_REVISION,'chunk_no':r['chunk_no'],'cosine_distance':float(r['distance']),
                         'priority':'highest_personal_reference','user_report_not_medical_fact':True}for r in hits]
