"""Truthful public wording while write tools still hold pending proposals.

Only narrow first-person/application completion claims are changed. The raw
provider tool protocol remains private; ordinary advice, quoted/history text
and JSON structured output are left alone. This is a bounded presentation
guard, never a substitute for executing and checking a confirmed write.
"""
from __future__ import annotations
import re

_LEAD=r'(?P<lead>\s*(?:[-*#✓✔✅\ufe0f]\s*)*(?:好的[，,]\s*)?)'
_ACTOR=r'(?:(?:我)?(?:已经|已|现已|成功)(?:帮你|为你|给你)?|我(?:帮你|为你|给你))'
_ACTION=r'(?:保存|记录|记下|录入|添加|创建|设置|设好)(?:好了|完成了|成功了|下来了|好|完成|成功|了)?'
_CLAIM=re.compile(r'^'+_LEAD+_ACTOR+r'(?:把[^，,。！？\n]{1,32}?)?'+_ACTION)
_BOUNDARY=re.compile(r'[。！？!?\n]')

class PersistenceTextGuard:
    """At most 256 characters wait for a sentence boundary; no token retry."""
    def __init__(self, pending):
        self.pending=pending
        self.buffer=''

    def _sentence(self,value):
        match=_CLAIM.match(value)
        if match is None:return value
        wording=('已整理为待确认内容，尚未保存：' if self.pending() else
                 '本次尚未保存，整理内容：')
        suffix=value[match.end():]
        if not suffix.strip('。！？!?\n \t，,：:'):
            wording=wording.rstrip('：')
        return match['lead']+wording+suffix

    def feed(self,value):
        self.buffer+=value;output=[]
        while self.buffer:
            boundary=_BOUNDARY.search(self.buffer)
            if boundary is not None and boundary.end()<=256:end=boundary.end()
            elif len(self.buffer)>=256:end=256
            else:break
            output.append(self._sentence(self.buffer[:end]));self.buffer=self.buffer[end:]
        return ''.join(output)

    def finish(self):
        value=self._sentence(self.buffer);self.buffer='';return value
