"""Milvus research retrieval, with stock/date provenance and deterministic chunk IDs."""
from __future__ import annotations

import hashlib
import json
import math
import os
import logging
from datetime import datetime, timezone
from typing import Any
from .http import HttpClient

logger=logging.getLogger(__name__)


class ResearchIndex:
    def __init__(self, settings):
        self.settings = settings
        self.config = settings.values.get('research', {})
        self._client = None

    @property
    def enabled(self):
        return bool(self.config.get('enabled'))

    @property
    def collection(self):
        model = self.config.get('embedding_model', 'zh-char-bigram-v1')
        suffix = hashlib.sha256(f"{model}:{self.dimension}".encode()).hexdigest()[:10]
        return f"{self.config.get('collection', 'a_stock_research')}_{suffix}"

    @property
    def dimension(self):
        return int(self.config.get('dimension', 384))

    @property
    def timeout(self):
        return float(self.config.get('timeout_seconds',5))

    def embed(self, text):
        endpoint = str(self.config.get('embedding_base_url', '')).rstrip('/')
        if endpoint:
            import urllib.request
            key = os.getenv(self.config.get('embedding_key_env', 'ASTOCK_EMBEDDING_API_KEY'), '')
            request = urllib.request.Request(endpoint+'/embeddings', data=json.dumps({
                'model': self.config['embedding_model'], 'input': text,
            }).encode(), headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                vector = json.load(response)['data'][0]['embedding']
            if len(vector) != self.dimension or not all(math.isfinite(float(v)) for v in vector):
                raise ValueError('Embedding dimension/value mismatch')
            return vector
        # Local lexical embeddings; no claim of semantic understanding, no model download.
        vector = [0.0]*self.dimension
        text = ''.join(text.lower().split())
        for token in [text[i:i+2] for i in range(max(len(text)-1, 1))]:
            value = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], 'big')
            vector[value % self.dimension] += 1.0 if value & 1 else -1.0
        norm = math.sqrt(sum(v*v for v in vector)) or 1
        return [v/norm for v in vector]

    def client(self):
        if not self.enabled:
            raise ValueError('Milvus research retrieval is disabled')
        if self._client is None:
            logger.info('research client initializing')
            from .milvus_service import ensure_local_milvus
            ensure_local_milvus(self.settings)
            logger.info('research service ready; importing pymilvus')
            from pymilvus import MilvusClient
            logger.info('research connecting to Milvus')
            self._client = MilvusClient(
                uri=os.getenv('ASTOCK_MILVUS_URI') or self.config.get('uri', 'http://127.0.0.1:19530'),
                token=os.getenv('ASTOCK_MILVUS_TOKEN', ''), timeout=float(self.config.get('timeout_seconds', 5)),
            )
            logger.info('research Milvus client connected')
        return self._client

    def ensure_collection(self):
        from pymilvus import DataType
        client = self.client()
        logger.info('research checking collection for search')
        if not client.has_collection(self.collection,timeout=self.timeout):
            schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field('id', DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field('vector', DataType.FLOAT_VECTOR, dim=self.dimension)
            schema.add_field('code', DataType.VARCHAR, max_length=6)
            schema.add_field('published_at', DataType.INT64)
            schema.add_field('title', DataType.VARCHAR, max_length=1024)
            schema.add_field('source', DataType.VARCHAR, max_length=2048)
            schema.add_field('text', DataType.VARCHAR, max_length=16000)
            indexes = client.prepare_index_params()
            indexes.add_index('vector', index_type='AUTOINDEX', metric_type='COSINE')
            client.create_collection(self.collection, schema=schema, index_params=indexes, consistency_level='Strong',timeout=self.timeout)
        return client

    @staticmethod
    def timestamp(value):
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('研报发布时间必须包含时区')
        return int(parsed.timestamp())

    def ingest(self, documents):
        rows = []
        for doc in documents:
            code = str(doc.get('code', ''))
            if code and (len(code)!=6 or not code.isdigit()):
                raise ValueError('Invalid research stock code')
            published = self.timestamp(doc['published_at'])
            title, source, text = (str(doc[k]).strip() for k in ('title','source','text'))
            if not title or not source or not text:
                raise ValueError('研报必须包含标题、来源和正文')
            if len(title.encode())>1024 or len(source.encode())>2048:
                raise ValueError('Research metadata exceeds Milvus field length')
            for offset in range(0, len(text), 800):
                chunk = text[offset:offset+1000]
                identifier = hashlib.sha256(f'{code}:{source}:{published}:{offset}:{chunk}'.encode()).hexdigest()
                rows.append(dict(id=identifier,vector=self.embed(title+' '+chunk),code=code,published_at=published,title=title,source=source,text=chunk))
        # Milvus Lite permits concurrent readers, but requires serialized writers.
        from .milvus_service import startup_lock
        with startup_lock(self.settings.runtime/'state/research_write.lock'):
            client = self.ensure_collection()
            for offset in range(0,len(rows),100):
                client.upsert(collection_name=self.collection, data=rows[offset:offset+100],timeout=self.timeout)
        return {'collection': self.collection, 'chunks': len(rows)}

    def search(self, query, code, as_of, limit=3):
        if not self.enabled:
            return {'status': 'disabled', 'hits': []}
        cutoff = self.timestamp(as_of)
        client = self.client()
        if not client.has_collection(self.collection,timeout=self.timeout):
            return {'status':'empty_collection', 'hits':[]}
        client.load_collection(self.collection,timeout=self.timeout)
        logger.info('research collection loaded; searching')
        result = client.search(collection_name=self.collection, data=[self.embed(query)],
            filter=f'(code == "{code}" or code == "") and published_at <= {cutoff}',
            limit=limit, search_params={'metric_type':'COSINE'}, output_fields=['code','published_at','title','source','text'],timeout=self.timeout)
        hits = [{'id':hit['id'],'similarity':hit['distance'],**hit['entity']} for hit in result[0]]
        logger.info('research search completed: %s hits',len(hits))
        return {'status':'ok','hits':hits,'collection':self.collection,'as_of':as_of}

    def status(self):
        if not self.enabled:return {'status':'disabled','chunks':0}
        client=self.client()
        exists=client.has_collection(self.collection,timeout=self.timeout)
        stats=client.get_collection_stats(self.collection,timeout=self.timeout) if exists else {}
        return {'status':'running','collection':self.collection,'chunks':int(stats.get('row_count',0)),
                'embedding':'本地中文文本向量' if not self.config.get('embedding_base_url') else '语义向量模型'}

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
