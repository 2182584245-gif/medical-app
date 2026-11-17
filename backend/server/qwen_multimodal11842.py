"""Bounded server-only Qwen multimodal HTTP client.

Public contracts: help.aliyun.com/zh/model-studio/multimodal-embedding-api-reference
and help.aliyun.com/zh/model-studio/text-rerank-api. No SDK, proxy discovery,
redirects, automatic retries, model downloads, or request/response logging.
"""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import base64, hashlib, http.client, json, math, os, re, ssl, stat, struct, threading, time
import urllib.error, urllib.request

EMBEDDING_MODEL='qwen3-vl-embedding'
RERANK_MODEL='qwen3-vl-rerank'
DIMENSIONS=512
QUERY_INSTRUCTION="Given a user's question, retrieve relevant authorized personal records and images."
DOCUMENT_INSTRUCTION='Represent this personal record or image for retrieval.'
RERANK_INSTRUCTION='Given a user question, rank authorized personal records and images that provide relevant evidence.'
CONTRACT={
    'schema':'fawnsee-qwen-mm-http-v1','model':EMBEDDING_MODEL,'dimension':DIMENSIONS,
    'output_type':'dense','normalization':'l2-finite-float','independent_order':'strict-input-index',
    'query_instruction':QUERY_INSTRUCTION,'document_instruction':DOCUMENT_INSTRUCTION,
    'fusion':'explicit-enable_fusion-text-then-image','image':'private-png-jpeg-webp-validated-header-v1',
    'response_types':{'independent':['vl','matching-text-or-image'],'fusion':['fusion']},
}
# This identifies our processing contract, not the provider's rolling weights.
MODEL_REVISION=hashlib.sha256(json.dumps(CONTRACT,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:40]
CONTRACT_ID=EMBEDDING_MODEL+':'+str(DIMENSIONS)+':'+MODEL_REVISION
MAX_TEXT_CHARS=4500
MAX_TEXT_BATCH_BYTES=24000
MAX_EMBED_ITEMS=20
MAX_IMAGE_BYTES=2*1024*1024
MAX_IMAGE_PIXELS=4*1024*1024
MAX_RERANK_DOCUMENTS=20
MAX_RERANK_IMAGES=4
MAX_RERANK_IMAGE_BYTES=6*1024*1024
MAX_RERANK_TEXT_BYTES=60000
MAX_REQUEST_BYTES=9*1024*1024
MAX_RESPONSE_BYTES=1024*1024
MAX_TIMEOUT_SECONDS=30.0
LOCK_WAIT_SECONDS=5.0
_NETWORK_LOCK=threading.Lock()
_BASE_PATTERN=re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cn-beijing\.maas\.aliyuncs\.com/api/v1')
_EMBED_PATH='/services/embeddings/multimodal-embedding/multimodal-embedding'
_RERANK_PATH='/services/rerank/text-rerank/text-rerank'

class QwenClientError(RuntimeError):pass
class QwenConfigurationError(QwenClientError):pass
class QwenProtocolError(QwenClientError):pass
class QwenBusy(QwenClientError):pass
class QwenAPIError(QwenClientError):
    def __init__(self,code,*,http_status=None):
        super().__init__(code);self.http_status=http_status

def _finite_number(value):
    if type(value)not in(int,float):return False
    try:return math.isfinite(value)
    except (OverflowError,ValueError):return False

def _setting(name,maximum):
    value=os.environ.get(name);filename=os.environ.get(name+'_FILE')
    if value is not None and filename is not None:raise QwenConfigurationError('ambiguous_server_configuration')
    if filename is not None:
        descriptor=None
        try:
            if not filename or len(filename)>4096 or '\x00'in filename:raise ValueError
            path=Path(filename)
            if not path.is_absolute()or path.is_symlink():raise ValueError
            descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            info=os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode)or info.st_size>maximum+2:raise ValueError
            if os.name=='posix'and stat.S_IMODE(info.st_mode)&0o077:raise ValueError
            with os.fdopen(descriptor,'rb')as stream:
                descriptor=None;raw=stream.read(maximum+3)
            value=raw.decode('utf-8').rstrip('\r\n')
        except (OSError,ValueError,UnicodeError):raise QwenConfigurationError('server_configuration_file_unavailable')from None
        finally:
            if descriptor is not None:os.close(descriptor)
    if type(value)is not str or not value or len(value)>maximum:raise QwenConfigurationError('server_configuration_missing_or_invalid')
    return value

def _text(value):
    if type(value)is not str or not value.strip()or len(value)>MAX_TEXT_CHARS:raise ValueError('Invalid Qwen text')
    try:encoded=value.encode('utf-8')
    except UnicodeError:raise ValueError('Invalid Qwen text')from None
    if len(encoded)>MAX_TEXT_CHARS*4:raise ValueError('Invalid Qwen text')
    return value,len(encoded)

def _image_shape(data,mime):
    """Read bounded common-format headers without decoding images in the API worker."""
    if type(data)is not bytes or not 1<=len(data)<=MAX_IMAGE_BYTES:raise ValueError('Invalid Qwen image bytes')
    width=height=0
    if mime=='image/png'and data.startswith(b'\x89PNG\r\n\x1a\n')and len(data)>=33 and data[12:16]==b'IHDR':
        width,height=struct.unpack('>II',data[16:24])
    elif mime=='image/jpeg'and data.startswith(b'\xff\xd8')and data.endswith(b'\xff\xd9'):
        cursor=2
        while cursor+4<=len(data):
            if data[cursor]!=255:break
            while cursor<len(data)and data[cursor]==255:cursor+=1
            if cursor>=len(data):break
            marker=data[cursor];cursor+=1
            if marker in(0xD8,0xD9,0x01)or 0xD0<=marker<=0xD7:continue
            if cursor+2>len(data):break
            size=int.from_bytes(data[cursor:cursor+2],'big')
            if size<2 or cursor+size>len(data):break
            if marker in(0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF)and size>=8:
                height,width=struct.unpack('>HH',data[cursor+3:cursor+7]);break
            if marker==0xDA:break
            cursor+=size
    elif mime=='image/webp'and len(data)>=30 and data[:4]==b'RIFF'and data[8:12]==b'WEBP':
        kind=data[12:16]
        if kind==b'VP8X'and not data[20]&0x02:
            width=1+int.from_bytes(data[24:27],'little');height=1+int.from_bytes(data[27:30],'little')
        elif kind==b'VP8 'and data[23:26]==b'\x9d\x01\x2a':
            width=int.from_bytes(data[26:28],'little')&0x3fff;height=int.from_bytes(data[28:30],'little')&0x3fff
        elif kind==b'VP8L'and data[20]==0x2f:
            bits=int.from_bytes(data[21:25],'little');width=1+(bits&0x3fff);height=1+((bits>>14)&0x3fff)
    if not width or not height or width*height>MAX_IMAGE_PIXELS:raise ValueError('Invalid or oversized Qwen image dimensions')
    return width*height

def _data_uri(data,mime):
    _image_shape(data,mime)
    return 'data:'+mime+';base64,'+base64.b64encode(data).decode('ascii')

def _json_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('Duplicate JSON member')
        value[key]=item
    return value

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise QwenAPIError('redirect_refused')

class QwenMultimodalClient:
    contract_id=CONTRACT_ID
    model_revision=MODEL_REVISION
    dimensions=DIMENSIONS

    def __init__(self,*,api_key,base_url,timeout=MAX_TIMEOUT_SECONDS,_opener=None):
        if type(base_url)is not str or _BASE_PATTERN.fullmatch(base_url)is None:raise QwenConfigurationError('official_workspace_endpoint_required')
        if type(api_key)is not str or re.fullmatch(r'[A-Za-z0-9._:-]{8,4096}',api_key)is None:raise QwenConfigurationError('server_api_key_invalid')
        if not _finite_number(timeout)or not 0<timeout<=MAX_TIMEOUT_SECONDS:raise QwenConfigurationError('bounded_timeout_required')
        self._base_url=base_url;self._api_key=api_key;self._timeout=float(timeout)
        self._opener=_opener if _opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}),_NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def __repr__(self):return '<QwenMultimodalClient model=qwen3-vl-embedding dimensions=512>'

    @classmethod
    def from_env(cls):
        return cls(api_key=_setting('QWEN_VL_API_KEY',4096),base_url=_setting('QWEN_VL_BASE_URL',512))

    def ready(self):
        """Configuration only; never issue a paid readiness probe."""
        return True

    def _remaining(self,deadline):
        if deadline is None:return self._timeout
        if not _finite_number(deadline):raise ValueError('Invalid Qwen deadline')
        remaining=deadline-time.monotonic()
        if remaining<=0:raise QwenBusy('qwen_deadline_expired')
        return min(self._timeout,remaining)

    @contextmanager
    def _exclusive(self,deadline):
        remaining=self._remaining(deadline)
        if not _NETWORK_LOCK.acquire(timeout=min(LOCK_WAIT_SECONDS,remaining)):raise QwenBusy('qwen_concurrency_budget_unavailable')
        try:
            self._remaining(deadline);yield
        finally:_NETWORK_LOCK.release()

    def _post_json(self,path,payload,*,pre_send,deadline):
        if path not in(_EMBED_PATH,_RERANK_PATH):raise QwenConfigurationError('qwen_endpoint_not_allowed')
        if pre_send is not None and not callable(pre_send):raise ValueError('Invalid Qwen authorization callback')
        encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        if len(encoded)>MAX_REQUEST_BYTES:raise ValueError('Qwen request exceeds byte budget')
        url=self._base_url+path
        request=urllib.request.Request(url,data=encoded,method='POST',headers={
            'Authorization':'Bearer '+self._api_key,'Content-Type':'application/json',
            'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'FAWNSEE-QWEN11842/1'})
        if pre_send is not None:
            allowed=pre_send()
            if allowed is not None and allowed is not True:raise PermissionError('Qwen request authorization denied')
        call_deadline=time.monotonic()+self._remaining(deadline)
        try:
            with self._opener.open(request,timeout=self._remaining(call_deadline))as response:
                if response.geturl()!=url:raise QwenAPIError('response_endpoint_changed')
                if response.status!=200:raise QwenAPIError('http_status_'+str(int(response.status)),http_status=int(response.status))
                if response.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':raise QwenProtocolError('json_content_type_required')
                chunks=[];length=0
                while True:
                    remaining=self._remaining(call_deadline)
                    sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                    if sock is not None:sock.settimeout(remaining)
                    chunk=getattr(response,'read1',response.read)(min(65536,MAX_RESPONSE_BYTES+1-length))
                    self._remaining(call_deadline)
                    if not chunk:break
                    chunks.append(chunk);length+=len(chunk)
                    if length>MAX_RESPONSE_BYTES:raise QwenProtocolError('qwen_response_exceeds_byte_budget')
                raw=b''.join(chunks)
        except urllib.error.HTTPError as error:
            status=error.code;error.close();raise QwenAPIError('http_status_'+str(status),http_status=status)from None
        except QwenClientError:raise
        except (OSError,TimeoutError,urllib.error.URLError,http.client.HTTPException):raise QwenAPIError('qwen_network_unavailable')from None
        try:
            value=json.loads(raw,object_pairs_hook=_json_object,parse_constant=lambda _:(_ for _ in()).throw(ValueError()))
        except (ValueError,UnicodeError,RecursionError):raise QwenProtocolError('qwen_invalid_json_response')from None
        if type(value)is not dict:raise QwenProtocolError('qwen_response_object_required')
        if 'code'in value:raise QwenAPIError('qwen_provider_error')
        if 'model'in value and value['model']!=payload['model']:raise QwenProtocolError('qwen_response_model_differs')
        return value

    def _embeddings(self,contents,kinds,*,query,fusion=False,pre_send=None,deadline=None):
        parameters={'dimension':DIMENSIONS,'output_type':'dense','enable_fusion':fusion,
                    'instruct':QUERY_INSTRUCTION if query else DOCUMENT_INSTRUCTION}
        value=self._post_json(_EMBED_PATH,{'model':EMBEDDING_MODEL,'input':{'contents':contents},'parameters':parameters},pre_send=pre_send,deadline=deadline)
        output=value.get('output');rows=output.get('embeddings')if type(output)is dict else None
        if type(rows)is not list or len(rows)!=len(kinds):raise QwenProtocolError('qwen_embedding_count_differs')
        ordered=[None]*len(kinds)
        for row in rows:
            if type(row)is not dict:raise QwenProtocolError('qwen_embedding_object_required')
            index=row.get('index')
            if type(index)is not int or not 0<=index<len(kinds)or ordered[index]is not None:raise QwenProtocolError('qwen_embedding_index_differs')
            allowed_types=('fusion',)if fusion else ('vl',kinds[index])
            if row.get('type')not in allowed_types:raise QwenProtocolError('qwen_embedding_modality_differs')
            vector=row.get('embedding')
            if type(vector)is not list or len(vector)!=DIMENSIONS or any(not _finite_number(x)for x in vector):raise QwenProtocolError('qwen_embedding_vector_invalid')
            try:norm=math.hypot(*vector)
            except (OverflowError,ValueError):raise QwenProtocolError('qwen_embedding_norm_invalid')from None
            if not math.isfinite(norm)or norm<=0:raise QwenProtocolError('qwen_embedding_norm_invalid')
            ordered[index]=[float(x/norm)for x in vector]
        return ordered

    def embed_text(self,text,*,query=False,pre_send=None,deadline=None):
        return self.embed_texts([text],query=query,pre_send=pre_send,deadline=deadline)[0]

    def embed_texts(self,texts,*,query=False,pre_send=None,deadline=None):
        if type(query)is not bool or type(texts)is not list or not 1<=len(texts)<=MAX_EMBED_ITEMS:raise ValueError('Invalid Qwen embedding batch')
        checked=[_text(text)for text in texts]
        if sum(size for _,size in checked)>MAX_TEXT_BATCH_BYTES:raise ValueError('Qwen text batch exceeds byte budget')
        with self._exclusive(deadline):
            return self._embeddings([{'text':text}for text,_ in checked],['text']*len(checked),query=query,pre_send=pre_send,deadline=deadline)

    def embed_image(self,image,*,mime_type='image/jpeg',text=None,pre_send=None,deadline=None):
        _image_shape(image,mime_type)
        if text is not None:_text(text)
        with self._exclusive(deadline):
            contents=[]if text is None else[{'text':text}]
            contents.append({'image':_data_uri(image,mime_type)})
            return self._embeddings(contents,['fusion'if text is not None else'image'],query=False,
                fusion=text is not None,pre_send=pre_send,deadline=deadline)[0]

    def rerank(self,query,documents,*,top_n=5,pre_send=None,deadline=None):
        query,query_bytes=_text(query)
        if type(documents)is not list or not 1<=len(documents)<=MAX_RERANK_DOCUMENTS:raise ValueError('Invalid Qwen rerank documents')
        if type(top_n)is not int or not 1<=top_n<=len(documents):raise ValueError('Invalid Qwen rerank top_n')
        texts=images=image_bytes=image_pixels=0
        for document in documents:
            if type(document)is str:texts+=_text(document)[1]
            elif type(document)is dict and set(document)=={'text'}:texts+=_text(document['text'])[1]
            elif type(document)is dict and set(document)=={'image','mime_type'}:
                image_pixels+=_image_shape(document['image'],document['mime_type']);images+=1;image_bytes+=len(document['image'])
            else:raise ValueError('Invalid Qwen rerank document modality')
        if images>MAX_RERANK_IMAGES or image_bytes>MAX_RERANK_IMAGE_BYTES or image_pixels>MAX_IMAGE_PIXELS:raise ValueError('Qwen rerank image budget exceeded')
        if texts+query_bytes*len(documents)>MAX_RERANK_TEXT_BYTES:raise ValueError('Qwen rerank text budget exceeded')
        with self._exclusive(deadline):
            payload_documents=[{'text':doc}if type(doc)is str else{'text':doc['text']}if'text'in doc else{'image':_data_uri(doc['image'],doc['mime_type'])}for doc in documents]
            payload={'model':RERANK_MODEL,'input':{'query':{'text':query},'documents':payload_documents},
                     'parameters':{'top_n':top_n,'return_documents':False,'instruct':RERANK_INSTRUCTION}}
            value=self._post_json(_RERANK_PATH,payload,pre_send=pre_send,deadline=deadline)
            output=value.get('output');rows=output.get('results')if type(output)is dict else None
            if type(rows)is not list or len(rows)!=top_n:raise QwenProtocolError('qwen_rerank_count_differs')
            result=[];seen=set();previous=math.inf
            for row in rows:
                if type(row)is not dict:raise QwenProtocolError('qwen_rerank_object_required')
                index=row.get('index');score=row.get('relevance_score')
                if type(index)is not int or not 0<=index<len(documents)or index in seen:raise QwenProtocolError('qwen_rerank_index_invalid')
                if not _finite_number(score)or not 0<=score<=1 or score>previous:raise QwenProtocolError('qwen_rerank_score_invalid')
                result.append({'index':index,'relevance_score':float(score)});seen.add(index);previous=score
            return result
