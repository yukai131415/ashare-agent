"""Tenant accounts and opaque cookie sessions. No public self-registration."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timedelta
from functools import lru_cache
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid

from .config import Settings
from .scheduler import SHANGHAI

ROLES={'admin':'管理员','manager':'经理','user':'普通用户'}


class AuthError(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status


def now_text():return datetime.now(SHANGHAI).isoformat(timespec='seconds')


def password_hash(password):
    if not isinstance(password,str) or not 10<=len(password)<=256:raise AuthError('密码长度需为10至256个字符。')
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,600_000)
    return f'pbkdf2_sha256$600000${salt.hex()}${digest.hex()}'


def password_matches(password,encoded):
    try:
        algorithm,iterations,salt,expected=encoded.split('$')
        if algorithm!='pbkdf2_sha256' or not isinstance(password,str) or len(password)>256:return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),bytes.fromhex(salt),int(iterations)).hex()
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError):return False


class AuthStore:
    def __init__(self,path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','manager','user')),active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
                csrf TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS login_failures(scope TEXT PRIMARY KEY,failures INTEGER NOT NULL,blocked_until TEXT);
              CREATE TABLE IF NOT EXISTS token_requests(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id),
                user_id TEXT NOT NULL REFERENCES users(id),model TEXT NOT NULL,purpose TEXT NOT NULL,
                started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL DEFAULT 'running');
              CREATE TABLE IF NOT EXISTS token_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL REFERENCES token_requests(id),transport TEXT NOT NULL,provider TEXT NOT NULL,
                input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,provider_request_id TEXT,
                recorded_at TEXT NOT NULL,UNIQUE(request_id,transport));
              CREATE INDEX IF NOT EXISTS token_tenant_user_date ON token_requests(tenant_id,user_id,started_at);
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:yield db
        finally:db.close()

    def initialized(self):
        with self.connection() as db:return bool(db.execute('SELECT 1 FROM users LIMIT 1').fetchone())

    def setup(self,username,password,display_name='',tenant_name='默认租户'):
        user=self._user_values(username,password,display_name,'admin','default')
        tenant_name=self._name(tenant_name)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM users LIMIT 1').fetchone():raise AuthError('系统已初始化，请登录。',409)
            db.execute('INSERT INTO tenants VALUES(?,?,?)',('default',tenant_name,now_text()))
            self._insert_user(db,user,None)
        return self.user(user['id'])

    @staticmethod
    def _name(value):
        name=str(value or '').strip()
        if not 1<=len(name)<=80:raise AuthError('名称长度需为1至80个字符。')
        return name

    def _user_values(self,username,password,display_name,role,tenant):
        username=str(username or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username):raise AuthError('账号需为3至64位字母、数字、下划线、点或横线。')
        if role not in ROLES:raise AuthError('无效角色。')
        return {'id':uuid.uuid4().hex,'tenant_id':tenant,'username':username,'display_name':self._name(display_name or username),
                'password_hash':password_hash(password),'role':role,'created_at':now_text()}

    @staticmethod
    def _insert_user(db,user,creator):
        try:
            db.execute('INSERT INTO users(id,tenant_id,username,display_name,password_hash,role,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',
                       tuple(user[k] for k in ('id','tenant_id','username','display_name','password_hash','role'))+(creator,user['created_at']))
        except sqlite3.IntegrityError as exc:raise AuthError('账号已存在或租户无效。',409) from exc

    def user(self,user_id):
        with self.connection() as db:
            row=db.execute('SELECT u.id,u.tenant_id,u.username,u.display_name,u.role,u.active,u.created_at,t.name tenant_name FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=?',(user_id,)).fetchone()
        return dict(row) if row else None

    def create_tenant(self,actor,name):
        if actor['role']!='admin':raise AuthError('仅管理员可创建租户。',403)
        result={'id':uuid.uuid4().hex,'name':self._name(name),'created_at':now_text()}
        with self.connection() as db:db.execute('INSERT INTO tenants VALUES(?,?,?)',tuple(result.values()))
        return result

    def tenants(self,actor):
        if actor['role']!='admin':raise AuthError('仅管理员可查看全部租户。',403)
        with self.connection() as db:return [dict(row) for row in db.execute('SELECT id,name,created_at FROM tenants ORDER BY created_at')]

    def create_user(self,actor,values):
        role=values.get('role','user');tenant=values.get('tenant_id') or actor['tenant_id']
        if actor['role'] not in ('admin','manager'):raise AuthError('没有创建账号的权限。',403)
        if actor['role']=='manager' and (role!='user' or tenant!=actor['tenant_id']):raise AuthError('经理只能在本租户创建普通用户。',403)
        user=self._user_values(values.get('username'),values.get('password'),values.get('display_name'),role,tenant)
        with self.connection() as db:self._insert_user(db,user,actor['id'])
        return self.user(user['id'])

    def users(self,actor):
        if actor['role'] not in ('admin','manager'):raise AuthError('没有查看账号列表的权限。',403)
        condition,args=('',()) if actor['role']=='admin' else ('WHERE u.tenant_id=?',(actor['tenant_id'],))
        with self.connection() as db:return [dict(row) for row in db.execute(f'SELECT u.id,u.tenant_id,u.username,u.display_name,u.role,u.active,u.created_at,t.name tenant_name FROM users u JOIN tenants t ON t.id=u.tenant_id {condition} ORDER BY u.created_at DESC',args)]

    def set_active(self,actor,user_id,active):
        target=self.user(user_id)
        if not target:raise AuthError('账号不存在。',404)
        if actor['role']!='admin' and not (actor['role']=='manager' and target['role']=='user' and target['tenant_id']==actor['tenant_id']):raise AuthError('没有管理该账号的权限。',403)
        if user_id==actor['id']:raise AuthError('不能在此停用自己的账号。')
        with self.connection() as db:
            db.execute('UPDATE users SET active=? WHERE id=?',(int(bool(active)),user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
        return self.user(user_id)

    def login(self,username,password,remote='local'):
        username=str(username or '').strip()
        scope=hashlib.sha256((remote+'|'+username.lower()).encode()).hexdigest()
        with self.connection() as db:
            fail=db.execute('SELECT * FROM login_failures WHERE scope=?',(scope,)).fetchone()
            if fail and fail['blocked_until'] and fail['blocked_until']>now_text():raise AuthError('登录尝试过多，请15分钟后重试。',429)
            row=db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(username,)).fetchone()
        valid=password_matches(password,row['password_hash']) if row else password_matches(password,'pbkdf2_sha256$600000$'+'00'*16+'$'+'00'*32)
        if not row or not row['active'] or not valid:
            with self.connection() as db:
                count=(fail['failures'] if fail and (not fail['blocked_until'] or fail['blocked_until']>now_text()) else 0)+1
                blocked=(datetime.now(SHANGHAI)+timedelta(minutes=15)).isoformat(timespec='seconds') if count>=8 else None
                db.execute('INSERT INTO login_failures VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET failures=excluded.failures,blocked_until=excluded.blocked_until',(scope,count,blocked))
            raise AuthError('账号或密码错误，或账号已停用。',401)
        raw=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute('DELETE FROM login_failures WHERE scope=?',(scope,))
            db.execute('DELETE FROM sessions WHERE expires_at<=?',(now_text(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),row['id'],csrf,(datetime.now(SHANGHAI)+timedelta(hours=8)).isoformat(timespec='seconds'),now_text()))
        return raw,{**self.user(row['id']),'csrf_token':csrf}

    def session(self,raw):
        if not raw or len(raw)>256:return None
        digest=hashlib.sha256(raw.encode()).hexdigest()
        with self.connection() as db:
            row=db.execute('SELECT user_id,csrf FROM sessions WHERE token_hash=? AND expires_at>?',(digest,now_text())).fetchone()
        if not row:return None
        user=self.user(row['user_id'])
        return {**user,'csrf_token':row['csrf']} if user and user['active'] else None

    def logout(self,raw):
        with self.connection() as db:db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),))


@lru_cache(maxsize=16)
def _store(runtime):
    from pathlib import Path
    return AuthStore(Path(runtime)/'security'/'accounts.db')


def auth_store(settings):return _store(str(settings.runtime.resolve()))


@dataclass(frozen=True,slots=True)
class TenantSettings(Settings):
    tenant_id:str=''
    @property
    def runtime(self):return self.root/'runtime'/'tenants'/self.tenant_id


def tenant_settings(settings,tenant_id):
    if tenant_id=='default':return settings
    if not re.fullmatch(r'[a-f0-9]{32}',tenant_id):raise AuthError('无效租户。',403)
    values=deepcopy(settings.values)
    values['llm']={**values.get('llm',{}),'api_key':'','api_key_env':'ASTOCK_LLM_API_KEY_TENANT_'+tenant_id.upper(),'enabled':False,'agent_explanations_enabled':False}
    values['workflow']={**values.get('workflow',{}),'llm_enabled':False}
    return TenantSettings(settings.root,values,tenant_id)
