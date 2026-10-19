"""Optional real Milvus round trip using a separate, explicitly synthetic test collection."""
import copy
import uuid
from a_stock_agents.config import load_settings,Settings
from a_stock_agents.contracts import iso_now
from a_stock_agents.research import ResearchIndex
from a_stock_agents.storage import atomic_write_json


def main():
    base=load_settings();values=copy.deepcopy(base.values)
    values['research'].update(enabled=True,collection='astock_smoke_'+uuid.uuid4().hex[:10])
    index=ResearchIndex(Settings(base.root,values))
    created=False
    try:
        # Clearly test fixtures, never merged into the real research collection.
        docs=[dict(code=code,published_at=day,title='合成测试文档',source='integration-test-only',text='测试盈利估值与风险')
              for code,day in [('000823','2026-10-15T10:00:00+08:00'),('000823','2026-10-20T10:00:00+08:00'),('600000','2026-10-15T10:00:00+08:00')]]
        inserted=index.ingest(docs);created=True
        result=index.search('盈利估值','000823','2026-10-19T14:30:00+08:00',10)
        assert len(result['hits'])==1,result
        assert result['hits'][0]['code']=='000823'
        report={'verified_at':iso_now(),'ok':True,'checks':['actual Milvus upsert/search','stock filter','future disclosure filter'],**inserted}
        atomic_write_json(base.root/'docs/verification/milvus_smoke.json',report)
        print(report)
    except Exception as exc:
        atomic_write_json(base.root/'docs/verification/milvus_smoke.json',{
            'verified_at':iso_now(),'ok':False,'error_type':type(exc).__name__,
            'note':'Actual Milvus round trip did not pass. Check the configured Milvus server; this is not a mock-test success.'})
        raise
    finally:
        if created:
            index.client().drop_collection(index.collection)
        index.close()


if __name__=='__main__':main()
