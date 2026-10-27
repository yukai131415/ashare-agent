import unittest
from html.parser import HTMLParser
from a_stock_agents.login_view import login_page


class FormFields(HTMLParser):
    def __init__(self):super().__init__();self.fields={};self.labels=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='input':self.fields[attrs.get('name')]=attrs
        if tag=='label':self.labels.append(attrs.get('for'))


class LoginViewTests(unittest.TestCase):
    def test_login_preserves_endpoint_and_validation(self):
        html=login_page();parser=FormFields();parser.feed(html)
        self.assertEqual(set(parser.fields),{'username','password'})
        self.assertEqual(parser.fields['password']['autocomplete'],'current-password')
        self.assertEqual(parser.fields['password']['type'],'password')
        self.assertEqual(parser.fields['password']['minlength'],'10')
        self.assertIn('data-endpoint="login"',html)
        self.assertIn('X-Requested-With',html)
        self.assertIn('可视化示意 · 非实时行情',html)
        self.assertIn('prefers-reduced-motion',html)
        self.assertNotIn('__CSS__',html);self.assertNotIn('__JS__',html)

    def test_setup_preserves_fields_without_public_signup(self):
        html=login_page(True);parser=FormFields();parser.feed(html)
        self.assertEqual(set(parser.fields),{'username','password','display_name','tenant_name'})
        self.assertEqual(parser.fields['password']['autocomplete'],'new-password')
        self.assertIn('data-endpoint="setup"',html)
        self.assertIn('创建管理员并进入',html)
        self.assertIn('首次设置仅支持本机操作',html)
        self.assertTrue(all(key in parser.labels for key in ('username','password','tenant-name','display-name')))


if __name__=='__main__':unittest.main()
