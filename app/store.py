import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 签字账：每笔复核签字(含复检材料)只增不改；outcome 当前=裁决生效版，留痕=有效但未成为当前。
    db.execute('''CREATE TABLE IF NOT EXISTS signatures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        reviewer TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL,
        material TEXT NOT NULL DEFAULT '',
        base_version INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(run_id,reviewer,decision,material))''')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:
        r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not r:raise DomainError('NOT_FOUND','批次不存在',404)
        cur=db.execute("SELECT reviewer,decision,material,base_version,created_at FROM signatures WHERE run_id=? AND outcome='当前'",(run_id,)).fetchone()
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    body['adjudication']=dict(cur) if cur else None
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def ledger(run_id):
    with connection() as db:
        if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
        rows=db.execute('SELECT id,reviewer,decision,material,base_version,outcome,created_at FROM signatures WHERE run_id=? ORDER BY id',(run_id,)).fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision, reviewer='', material=''):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    reviewer=(reviewer or '').strip();material=(material or '').strip()
    outcome=dup=None
    with connection() as db:
        # BEGIN IMMEDIATE 先取写锁再读写：两人同时提交被串行化，只留一个当前版本；
        # 版本号与签字账在同一事务提交，处理中断整体回滚、保留旧版。
        db.execute('BEGIN IMMEDIATE')
        run=db.execute('SELECT version FROM runs WHERE id=?',(run_id,)).fetchone()
        if not run:raise DomainError('NOT_FOUND','批次不存在',404)
        # 同一意见(同人同结论同复检材料)重试幂等：不新增版本、不新增签字账。
        dup=db.execute('SELECT outcome FROM signatures WHERE run_id=? AND reviewer=? AND decision=? AND material=?',(run_id,reviewer,decision,material)).fetchone()
        if dup:outcome='duplicate'
        else:
            current=db.execute("SELECT 1 FROM signatures WHERE run_id=? AND outcome='当前'",(run_id,)).fetchone()
            if current is None and run['version']==version:
                cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
                if cur.rowcount==1:
                    db.execute("INSERT INTO signatures(run_id,reviewer,decision,material,base_version,outcome) VALUES (?,?,?,?,?,'当前')",(run_id,reviewer,decision,material,version))
                    outcome='adopted'
            if outcome is None:
                # 先签先裁、一裁定局：未成为当前的签字仍属有效记录，连同复检材料一并留痕。
                db.execute("INSERT INTO signatures(run_id,reviewer,decision,material,base_version,outcome) VALUES (?,?,?,?,?,'留痕')",(run_id,reviewer,decision,material,version))
                outcome='stale' if current is None else 'conflict'
    if outcome in ('adopted','duplicate'):
        result=get(run_id)
        result['review_result']={'outcome':'当前' if outcome=='adopted' else dup['outcome'],'duplicate':outcome=='duplicate','reviewer':reviewer,'decision':decision,'material':material}
        return result
    if outcome=='stale':raise DomainError('STALE','复核版本已更新，请刷新；本次签字与复检材料已留痕',409)
    raise DomainError('CONFLICT','复核结论已裁决为当前版本，不再更改；本次签字与复检材料已留痕',409)
