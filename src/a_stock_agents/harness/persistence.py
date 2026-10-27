"""Durable run, attempt and checkpoint ledger. No credentials stored here."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from ..contracts import iso_now


def fingerprint(value):
    # 键排序消除字典插入顺序差异；拒绝 NaN，避免不可比较的输入进入恢复判断。
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,default=str).encode()).hexdigest()


class RunStore:
    def __init__(self,settings):
        self.path=settings.runtime/'harness/runs.db'

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,kind TEXT,version TEXT,input_json TEXT,input_hash TEXT,status TEXT,recoveries INTEGER DEFAULT 0,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS checkpoints(run_id TEXT,step_id TEXT,status TEXT,input_hash TEXT,output_json TEXT,attempts INTEGER DEFAULT 0,updated_at TEXT,PRIMARY KEY(run_id,step_id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run_id TEXT,step_id TEXT,at TEXT,status TEXT,payload TEXT);''')
            # with db 管提交/回滚，但不关闭连接；finally 显式关闭避免长期轮询泄漏。
            with db:yield db
        finally:db.close()

    def begin(self,run_id,plan,inputs):
        digest=fingerprint(inputs)
        with self.connection() as db:
            # 预先取得写锁，让“查重 + 创建”成为一个原子操作，避免并发绑定不同输入。
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            if existing and (existing['input_hash']!=digest or existing['version']!=plan['version'] or existing['kind']!=plan['task_type']):raise ValueError('运行编号已绑定其他输入或计划版本，拒绝复用')
            if not existing:db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,0,?)',(run_id,plan['task_type'],plan['version'],json.dumps(inputs,ensure_ascii=False),'%s'%digest,'running',iso_now()))

    def run(self,run_id):
        with self.connection() as db:
            row=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        return dict(row) if row else None

    def checkpoint(self,run_id,step_id):
        with self.connection() as db:row=db.execute('SELECT * FROM checkpoints WHERE run_id=? AND step_id=?',(run_id,step_id)).fetchone()
        return dict(row) if row else None

    def transition(self,run_id,step_id,status,input_hash='',output=None):
        # checkpoints 保存最新状态，events 保存变化轨迹；同事务防止两者不一致。
        # attempts 只在进入 running 时递增，验收和失败落库不算新的执行尝试。
        with self.connection() as db:
            db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?, ?,?) ON CONFLICT(run_id,step_id) DO UPDATE SET status=excluded.status,input_hash=excluded.input_hash,output_json=excluded.output_json,attempts=checkpoints.attempts+excluded.attempts,updated_at=excluded.updated_at',
                       (run_id,step_id,status,input_hash,json.dumps(output,ensure_ascii=False,allow_nan=False) if output is not None else None,1 if status=='running' else 0,iso_now()))
            db.execute('INSERT INTO events(run_id,step_id,at,status,payload) VALUES(?,?,?,?,?)',(run_id,step_id,iso_now(),status,json.dumps({'input_hash':input_hash},ensure_ascii=False)))

    def finish(self,run_id,status):
        with self.connection() as db:db.execute('UPDATE runs SET status=?,updated_at=? WHERE id=?',(status,iso_now(),run_id))

    def reserve_recovery(self,run_id,maximum):
        # 用带上限条件的单条 UPDATE 预占额度，而非先查再改，防止并发超预算。
        with self.connection() as db:
            updated=db.execute('UPDATE runs SET recoveries=recoveries+1,updated_at=? WHERE id=? AND recoveries<?',(iso_now(),run_id,maximum)).rowcount
        return bool(updated)

    def list_runs(self):
        with self.connection() as db:return [dict(r) for r in db.execute('SELECT id,kind,version,status,recoveries,updated_at FROM runs ORDER BY updated_at DESC LIMIT 50')]

    def uncertain_model_calls(self,run_id,step_id):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():return False
            return bool(db.execute("SELECT 1 FROM model_calls WHERE run_id=? AND step_id IS ? AND status IN ('running','sent','outcome_unknown') LIMIT 1",(run_id,step_id)).fetchone())

    def trace(self,run_id):
        run=self.run(run_id)
        if not run:raise ValueError('找不到运行记录')
        with self.connection() as db:
            steps=[dict(r) for r in db.execute('SELECT step_id,status,attempts,updated_at FROM checkpoints WHERE run_id=?',(run_id,))]
            models=[]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone():models=[dict(r) for r in db.execute('SELECT id,step_id,model,status,payload FROM model_calls WHERE run_id=?',(run_id,))]
        return {'run':{k:v for k,v in run.items() if k not in ('input_json',)},'steps':steps,'model_calls':models}
