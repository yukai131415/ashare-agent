import tempfile
import unittest
from pathlib import Path
from a_stock_agents.config import Settings, load_settings
from a_stock_agents.llm import save_config, llm_config, public_config


class ModelProfileTests(unittest.TestCase):
    def test_switch_keeps_secrets_separate_and_never_returns_them(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            first=save_config(settings,{'base_url':'https://one.example/v1','model':'model-a','api_key':'first-private-key','enabled':True})
            first_id=first['active_profile_id']
            second=save_config(settings,{'add_profile':True,'base_url':'https://two.example/v1','model':'model-b','api_key':'second-private-key'})
            self.assertNotIn('private-key',str(public_config(settings)))
            self.assertNotEqual(first_id,second['active_profile_id'])
            save_config(settings,{'select_profile_id':first_id})
            self.assertEqual(llm_config(settings)['api_key'],'first-private-key')
            self.assertEqual(llm_config(settings)['base_url'],'https://one.example/v1')
            save_config(settings,{'select_profile_id':second['active_profile_id']})
            self.assertEqual(llm_config(settings)['api_key'],'second-private-key')

    def test_new_model_does_not_inherit_key_and_unknown_id_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Settings(Path(folder),load_settings().values)
            save_config(settings,{'api_key':'keep-private','model':'first'})
            save_config(settings,{'add_profile':True,'base_url':'https://other.example/v1','model':'second'})
            self.assertFalse(llm_config(settings)['api_key'])
            with self.assertRaises(ValueError):
                save_config(settings,{'select_profile_id':'unknown'})


if __name__=='__main__':unittest.main()
