"""Bounded Qwen-Audio-ASR-Message adapter. No audio/transcript/credential logs.

Official run-task/result-generated/task-finished duplex contract. One ephemeral
websocket per recognition, no retries, context, persistent files or fallback.
"""
from __future__ import annotations
import asyncio,io,json,logging,os,re,stat,struct,wave
from dataclasses import dataclass,field
from pathlib import Path
from uuid import uuid4

MODEL='qwen-audio-3.1-asr-flash-message'
ENDPOINT=os.environ.get('QWEN_ASR_ENDPOINT','')
SECRET_ROOT=Path('/run/qwen-asr')
MAX_AUDIO_BYTES=2*1024*1024;MAX_TEXT=4000;MAX_EVENTS=2048;MAX_MESSAGE=256*1024;MAX_STREAM=4*1024*1024
TASK_TIMEOUT=80;START_TIMEOUT=12;CHUNK_BYTES=6400
_LOGGER=logging.Logger('fawnsee.qwen_asr.private_transport',level=logging.CRITICAL);_LOGGER.propagate=False;_LOGGER.addHandler(logging.NullHandler())
class QwenASRError(RuntimeError):
    def __init__(self,code='unavailable'):self.code=code;super().__init__(code)
def need(value):
    if not value:raise QwenASRError('invalid_response')
def unique(pairs):
    out={}
    for key,value in pairs:
        need(key not in out);out[key]=value
    return out
def text(value,*,empty=False):
    need(type(value)is str and len(value)<=MAX_TEXT and not any(ord(c)<32 and c not in '\n\t'for c in value));value.encode('utf8');need(empty or bool(value.strip()));return value.strip()
def pcm_from_wav(raw):
    need(type(raw)is bytes and 44<len(raw)<=MAX_AUDIO_BYTES and raw[:4]==b'RIFF'and raw[8:12]==b'WAVE'and struct.unpack_from('<I',raw,4)[0]+8==len(raw))
    with wave.open(io.BytesIO(raw),'rb')as audio:
        need((audio.getframerate(),audio.getnchannels(),audio.getsampwidth(),audio.getcomptype())==(16000,1,2,'NONE'));frames=audio.getnframes();need(0<frames<=60*16000);pcm=audio.readframes(frames+1);need(len(pcm)==frames*2)
    return pcm
@dataclass(frozen=True)
class QwenSettings:
    endpoint:str=ENDPOINT
    model:str=MODEL
    api_key:str=field(default='',repr=False)
    def __post_init__(self):
        need(bool(re.fullmatch(r'wss://ws-[a-z0-9]+\.[a-z0-9-]+\.maas\.aliyuncs\.com/api-ws/v1/inference', ENDPOINT)))
        need(self.endpoint==ENDPOINT and self.model==MODEL)
        need(type(self.api_key)is str and 1<=len(self.api_key)<=1024 and all(33<=ord(c)<=126 for c in self.api_key))
    @classmethod
    def from_material(cls,config_raw,key_raw):
        config=json.loads(config_raw,object_pairs_hook=unique);need(type(config)is dict and set(config)=={'endpoint','model'})
        return cls(endpoint=config['endpoint'],model=config['model'],api_key=key_raw.decode('ascii').strip())
    @classmethod
    def from_secrets(cls):
        def read(name,limit,private=False):
            root=SECRET_ROOT;path=root/name
            need(root.is_dir()and not root.is_symlink()and root.resolve()==root and path.is_file()and not path.is_symlink()and path.resolve()==path)
            info=path.stat();need(stat.S_ISREG(info.st_mode)and info.st_size<=limit and not(info.st_mode&0o022))
            if private:need(not(info.st_mode&0o077))
            flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
            with os.fdopen(os.open(path,flags),'rb')as f:
                opened=os.fstat(f.fileno());need((opened.st_dev,opened.st_ino)==(info.st_dev,info.st_ino));raw=f.read(limit+1);need(len(raw)<=limit)
            return raw
        try:
            return cls.from_material(read('config.json',2048),read('api-key',2048,True))
        except Exception:raise QwenASRError('unavailable')from None

class Sentences:
    """Replace revisions by ID. Final text is never assembled from guesses."""
    def __init__(self):self.rows={};self.previous=''
    def update(self,sentence):
        need(type(sentence)is dict)
        if sentence.get('heartbeat')is True:return None
        identifier=sentence.get('sentence_id');need(type(identifier)is int and 1<=identifier<=256)
        ended=sentence.get('sentence_end');need(type(ended)is bool)
        value=text(sentence.get('text'),empty=not ended)
        previous=self.rows.get(identifier)
        if previous and previous['final']:
            need(ended and previous['text']==value)
            return None
        self.rows[identifier]={'text':value,'final':ended}
        current=''.join(self.rows[key]['text']for key in sorted(self.rows));need(len(current)<=MAX_TEXT)
        if not current or current==self.previous:return None
        self.previous=current;return current
    def final(self):
        need(bool(self.rows)and all(row['final']or not row['text']for row in self.rows.values()))
        return text(''.join(self.rows[key]['text']for key in sorted(self.rows)if self.rows[key]['final']))

class QwenASRAdapter:
    def __init__(self,*,settings=None,settings_loader=None,connect=None):
        self.settings=settings;self.settings_loader=settings_loader or QwenSettings.from_secrets;self.connect=connect
    async def events(self,wav):
        sender=None
        try:
            pcm=pcm_from_wav(wav);settings=self.settings or await asyncio.to_thread(self.settings_loader)
            connector=self.connect
            if connector is None:
                from websockets.asyncio.client import connect
                connector=connect
            task_id=str(uuid4());ledger=Sentences();count=total=0
            def event(raw):
                nonlocal count,total
                need(type(raw)is str);size=len(raw.encode('utf8'));count+=1;total+=size;need(size<=MAX_MESSAGE and count<=MAX_EVENTS and total<=MAX_STREAM)
                row=json.loads(raw,object_pairs_hook=unique);need(type(row)is dict and type(row.get('header'))is dict);header=row['header'];need(header.get('task_id')==task_id)
                if header.get('event')=='task-failed':raise QwenASRError('unavailable')
                return header.get('event'),row.get('payload')
            async with asyncio.timeout(TASK_TIMEOUT):
                async with connector(settings.endpoint,additional_headers={'Authorization':'Bearer '+settings.api_key},open_timeout=8,close_timeout=2,max_size=MAX_MESSAGE,max_queue=8,write_limit=32768,ping_interval=20,ping_timeout=20,compression=None,proxy=None,logger=_LOGGER)as ws:
                    run={'header':{'action':'run-task','task_id':task_id,'streaming':'duplex'},'payload':{'task_group':'audio','task':'asr','function':'recognition','model':MODEL,'parameters':{'format':'pcm','sample_rate':16000,'intermediate_result_enabled':True,'disfluency_removal_enabled':False,'keep_dialect':False,'vad_model':'near_meeting_16k','language_hints':['zh']},'input':{}}}
                    await ws.send(json.dumps(run,separators=(',',':')))
                    kind,payload=event(await asyncio.wait_for(ws.recv(),START_TIMEOUT));need(kind=='task-started')
                    yield {'type':'status','status':'recognizing'}
                    finish_sent=asyncio.Event()
                    async def send_audio():
                        try:
                            for offset in range(0,len(pcm),CHUNK_BYTES):await ws.send(pcm[offset:offset+CHUNK_BYTES]);await asyncio.sleep(0)
                            await ws.send(json.dumps({'header':{'action':'finish-task','task_id':task_id,'streaming':'duplex'},'payload':{'input':{}}},separators=(',',':')));finish_sent.set()
                        except BaseException:
                            await ws.close();raise
                    sender=asyncio.create_task(send_audio())
                    try:
                        while True:
                            kind,payload=event(await ws.recv())
                            if kind=='result-generated':
                                need(type(payload)is dict and type(payload.get('output'))is dict);current=ledger.update(payload['output'].get('sentence'))
                                if current:yield {'type':'partial','text':current}
                            elif kind=='task-finished':
                                await sender;need(finish_sent.is_set());yield {'type':'final','text':ledger.final(),'provider':'qwen','provider_model':MODEL,'offline':False};break
                            else:raise QwenASRError('invalid_response')
                    finally:
                        sender.cancel();await asyncio.gather(sender,return_exceptions=True)
        except asyncio.CancelledError:raise
        except GeneratorExit:raise
        except QwenASRError:raise
        except TimeoutError:raise QwenASRError('timeout')from None
        except Exception:raise QwenASRError('unavailable')from None
