import json
import unittest
from a_stock_agents.llm_context import question_evidence
from a_stock_agents.llm import _extract_answer,_build_request


class QuestionContextTests(unittest.TestCase):
    def test_truncated_output_is_explicitly_marked(self):
        result=_extract_answer({'choices':[{'finish_reason':'length','message':{'content':'尚未完成的回答'}}]},'openai_compatible')
        self.assertIn('尚未完整生成',result)
        normal=_extract_answer({'choices':[{'finish_reason':'stop','message':{'content':'完整回答'}}]},'openai_compatible')
        self.assertEqual(normal,'完整回答')
        result=_extract_answer({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[{'type':'message','content':[{'type':'output_text','text':'开头'}]}]},'openai_responses')
        self.assertIn('尚未完整生成',result)

    def test_aliyun_qwen_interactive_request_disables_thinking(self):
        _,payload,_=_build_request('https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1','测试',{'model':'qwen3.8-max'})
        self.assertIs(json.loads(payload)['enable_thinking'],False)
        _,payload,_=_build_request('https://other.example/v1','测试',{'model':'qwen3.8-max'})
        self.assertNotIn('enable_thinking',json.loads(payload))

    def test_drops_full_market_diagnostics_and_unrelated_history(self):
        rec={'code':'600001','name':'示例','score':1.23,'basis':{'news_factor_enabled':False}}
        data={'selected_date':'2026-10-22','recommendation':rec,'strategy':{},
              'workflow':{'agents':{'market':['raw-data']*10000},'roles':{'risk':{'status':'success','warnings':['波动风险'],'freshness':{'rows':['raw-data']*10000}},'fundamental':{'evidence':[{'code':'other','report':'unrelated'},{'code':'600001','report':'selected'}]}}},
              'backtest':{'wins':8,'losses':2,'results':[{'code':'other'}]*10000}}
        encoded=question_evidence(data,[]);context=json.loads(encoded)
        self.assertLess(len(encoded),18000);self.assertNotIn('raw-data',encoded);self.assertNotIn('unrelated',encoded)
        self.assertEqual(context['recommendation'],rec);self.assertEqual(context['backtest']['wins'],8)
        self.assertIn('selected',encoded);self.assertIn('已禁用',context['news_scoring_note'])

    def test_empty_model_content_is_not_a_successful_answer(self):
        for content in (None,'',[], '  '):
            with self.assertRaises(ValueError):_extract_answer({'choices':[{'message':{'content':content}}]},'openai_compatible')
        self.assertEqual(_extract_answer({'choices':[{'message':{'content':[{'type':'text','text':'真实回答'}]}}]},'openai_compatible'),'真实回答')


if __name__=='__main__':unittest.main()
