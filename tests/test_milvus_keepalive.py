import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a_stock_agents.config import Settings, load_settings
from a_stock_agents.research import ResearchIndex


class MilvusKeepaliveTests(unittest.TestCase):
    def test_client_uses_conservative_keepalive_and_releases_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            settings.values['research']['enabled']=True
            index=ResearchIndex(settings)
            with patch('pymilvus.MilvusClient') as client,patch('a_stock_agents.milvus_service.ensure_local_milvus'):
                first=index.client()
                self.assertIs(first,index.client())
                self.assertEqual(client.call_count,1)
                options=client.call_args.kwargs['grpc_options']
                self.assertEqual(options['grpc.keepalive_time_ms'],600_000)
                self.assertFalse(options['grpc.keepalive_permit_without_calls'])
                self.assertEqual(client.call_args.kwargs['timeout'],index.timeout)
                index.close()
                first.close.assert_called_once()
                self.assertIsNone(index._client)
