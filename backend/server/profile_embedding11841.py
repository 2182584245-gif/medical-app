"""Compatibility facade for the server-only Qwen multimodal embedding API."""
from __future__ import annotations
from .qwen_multimodal11842 import (
    QwenMultimodalClient,QwenClientError,EMBEDDING_MODEL as MODEL_ID,
    MODEL_REVISION,DIMENSIONS,CONTRACT_ID,
)

class EmbeddingUnavailable(RuntimeError):pass

class ChineseProfileEmbedder:
    """Retains the profile interface; missing Qwen configuration fails closed.

    `root` remains accepted for old constructor callers but is never read.
    Local model files, ONNX, tokenizers, and NumPy are no longer loaded.
    Readiness checks configuration only and cannot incur inference charges.
    """
    contract_id=CONTRACT_ID

    def __init__(self,root=None,*,client=None,pre_send=None):
        self._client=client;self._pre_send=pre_send

    def _get_client(self):
        if self._client is None:self._client=QwenMultimodalClient.from_env()
        return self._client

    def embed(self,text,*,query=False,pre_send=None,deadline=None):
        try:return self._get_client().embed_text(text,query=query,
            pre_send=self._pre_send if pre_send is None else pre_send,deadline=deadline)
        except QwenClientError:raise EmbeddingUnavailable('Qwen semantic embedding is unavailable')from None

    def embed_many(self,texts,*,query=False,pre_send=None,deadline=None):
        try:return self._get_client().embed_texts(texts,query=query,
            pre_send=self._pre_send if pre_send is None else pre_send,deadline=deadline)
        except QwenClientError:raise EmbeddingUnavailable('Qwen semantic embedding is unavailable')from None

    def ready(self):
        try:return self._get_client().ready()
        except QwenClientError:return False
