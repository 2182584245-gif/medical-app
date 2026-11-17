"""Recoverable account-erasure journal, separate from keys being destroyed."""
from __future__ import annotations
import hashlib,json,os,re
from pathlib import Path
from uuid import UUID


class ErasureJournal:
    def __init__(self,root,vault,endpoint):
        self.root=Path(root)/'client-erasure10516';self.vault=vault;self.endpoint=endpoint.rstrip('/')
        if self.root.is_symlink():raise ValueError('Invalid erasure directory')
        self.root.mkdir(parents=True,exist_ok=True)
        self.purpose='client-erasure10516/'+hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.path=self.root/(hashlib.sha256(self.endpoint.encode()).hexdigest()+'.sealed')
    def load(self):
        if self.path.is_symlink():raise ValueError('Invalid erasure journal')
        if not self.path.exists():return None
        if not self.path.is_file() or self.path.stat().st_size>50000:raise ValueError('Invalid erasure journal')
        value=json.loads(str(self.vault.openControl(self.purpose,self.path.read_text(encoding='utf8'))))
        if (type(value) is not dict or set(value)!={'actor','endpoint','request_id','bearer','phase'}
                or type(value['actor']) is not int or value['actor']<=0 or value['endpoint']!=self.endpoint
                or value['phase'] not in {'submitted','cloud_confirmed'}
                or str(UUID(value['request_id']))!=value['request_id']
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['bearer'])):raise ValueError('Invalid erasure journal')
        return value
    def save(self,value):
        temporary=self.path.with_suffix('.tmp')
        if self.path.is_symlink() or temporary.is_symlink():raise ValueError('Invalid erasure journal')
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':'))
        sealed=str(self.vault.sealControl(self.purpose,raw))
        if sealed==raw:raise ValueError('Unprotected erasure journal')
        with temporary.open('w',encoding='utf8') as stream:
            stream.write(sealed);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.path)
    def clear(self):
        for path in (self.path,self.path.with_suffix('.tmp')):
            if path.is_symlink():raise ValueError('Invalid erasure journal')
            path.unlink(missing_ok=True)


def clear_voice_files(root):
    """Only the app's fixed private recording directory; never follows links."""
    directory=Path(root)/'voice-capture'
    def remove(path):
        if path.is_symlink():raise ValueError('Linked recording directory')
        if not path.exists():return
        if path.is_dir():
            for child in path.iterdir():remove(child)
            path.rmdir()
        else:path.unlink()
    remove(directory)
