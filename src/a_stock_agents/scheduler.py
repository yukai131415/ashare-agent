"""Shanghai-time daily scheduler with catch-up, retry and process exclusion."""
from datetime import datetime, timedelta, timezone
import threading

from .console_log import console_event
from .storage import atomic_write_json, read_json
from .supervisor import Supervisor
from .trading_calendar import is_trading_weekday
from .harness.locking import process_lock

SHANGHAI = timezone(timedelta(hours=8))


def shanghai_now():
    return datetime.now(SHANGHAI)


def scheduler_display(config,state,now=None):
    """Read-only UI schedule using the scheduler's calendar and cutoff."""
    current=now or shanghai_now()
    current=current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    result={'server_now':current.isoformat(),'enabled':bool(config['enabled']),'next_run_at':None,'countdown_state':'disabled','next_kind':'daily'}
    if not result['enabled']:return result
    today=current.date().isoformat()
    try:
        trading=is_trading_weekday(current.date())
        cutoff=current.replace(hour=int(config['hour']),minute=int(config['minute']),second=0,microsecond=0)
        if trading and state.get('attempt_date')==today and state.get('status')=='running':
            result['countdown_state']='running';return result
        if trading and state.get('attempt_date')==today and state.get('status')=='needs_review':
            result['countdown_state']='needs_review';return result
        completed=state.get('completed_date')==today
        if trading and not completed and current>=cutoff:
            if state.get('attempt_date')==today and state.get('status')=='retry_wait':
                try:
                    retry=datetime.fromisoformat(state.get('next_retry_at',''))
                    retry=retry.replace(tzinfo=SHANGHAI) if retry.tzinfo is None else retry.astimezone(SHANGHAI)
                    if retry>current:
                        result.update(countdown_state='counting',next_kind='retry',next_run_at=retry.isoformat());return result
                except (TypeError,ValueError):pass
            result['countdown_state']='awaiting_dispatch';return result
        target=cutoff
        if completed or target<=current or not trading:target+=timedelta(days=1)
        while not is_trading_weekday(target.date()):target+=timedelta(days=1)
        result.update(countdown_state='counting',next_run_at=target.isoformat())
    except ValueError:
        result.update(countdown_state='calendar_unavailable',calendar_message='交易日历未配置，暂不推测下次执行日期。')
    return result


class DailyPipelineScheduler:
    def __init__(self, settings, config):
        self.settings = settings
        self.enabled = bool(config['enabled'])
        self.hour, self.minute = int(config['hour']), int(config['minute'])
        self._stop, self._running = threading.Event(), threading.Lock()
        self._thread = None
        self.path = settings.runtime / 'state' / 'scheduler_state.json'
        raw = settings.values.get('scheduler', {})
        self.poll_seconds = max(5, int(raw.get('check_interval_seconds', 30)))
        self.retry_seconds = max(30, int(raw.get('retry_seconds', 120)))

    def start(self):
        if not self.enabled:
            console_event('scheduler_disabled'); return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='daily-pipeline-scheduler', daemon=True)
        self._thread.start()
        console_event('scheduler_started', timezone='Asia/Shanghai', daily_run_time=f'{self.hour:02d}:{self.minute:02d}', check_interval_seconds=self.poll_seconds, retry_seconds=self.retry_seconds)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once('scheduled_daily')
                from .dashboard_enrichment import refresh_after_close_if_due
                refresh_after_close_if_due(self.settings)
                if self.settings.values.get('shadow',{}).get('enabled',False):
                    from .shadow import start_outcomes
                    start_outcomes(self.settings)
            except Exception as exc:
                console_event('scheduler_check_failed', error_type=type(exc).__name__, message=str(exc))
            self._stop.wait(self.poll_seconds)

    def _already_generated(self, now):
        health = read_json(self.settings.runtime / 'state' / 'health.json', {})
        rec = read_json(self.settings.runtime / 'agents' / 'recommendation' / 'data' / 'latest.json', {})
        today = now.date().isoformat()
        if not (health.get('ok') is True and not health.get('degraded') and str(health.get('finished_at', '')).startswith(today)):
            return False
        if str(rec.get('recommendation_date') or rec.get('created_at', ''))[:10] != today:
            return False
        try:
            stamp = datetime.fromisoformat(rec['created_at']).astimezone(SHANGHAI)
            return (stamp.hour, stamp.minute) >= (self.hour, self.minute)
        except (KeyError, ValueError, TypeError):
            return False

    def run_once(self, trigger, now=None):
        now = now or shanghai_now()
        now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
        if not self.enabled or not is_trading_weekday(now.date()) or (now.hour, now.minute) < (self.hour, self.minute):
            return False
        if not self._running.acquire(blocking=False):
            return False
        try:
            with process_lock(self.path.with_suffix('.lock')) as acquired:
                if not acquired:
                    return False
                state = read_json(self.path, {})
                today = now.date().isoformat()
                if state.get('completed_date') == today:
                    return False
                if self._already_generated(now):
                    state.update(completed_date=today, status='completed', last_trigger='existing_valid_result')
                    atomic_write_json(self.path, state)
                    console_event('scheduler_existing_result', date=today)
                    return False
                retry_at = state.get('next_retry_at')
                maximum=max(1,min(20,int(self.settings.values.get('harness',{}).get('max_daily_pipeline_attempts',3))))
                if state.get('attempt_date')==today and state.get('attempts',0)>=maximum:
                    if state.get('status')!='needs_review':
                        state.update(status='needs_review',last_error='当日任务已达到尝试上限，需要人工检查')
                        state.pop('next_retry_at',None);atomic_write_json(self.path,state)
                        console_event('scheduler_needs_review',level='WARN',date=today,attempts=state.get('attempts'))
                    return False
                if retry_at and state.get('attempt_date') == today and now < datetime.fromisoformat(retry_at):
                    return False
                state.update(status='running', attempt_date=today, last_run_at=now.isoformat(), last_trigger=trigger,
                             attempts=(state.get('attempts', 0) if state.get('attempt_date') == today else 0)+1)
                atomic_write_json(self.path, state)
                console_event('scheduler_run_started', date=today, trigger=trigger, attempt=state['attempts'])
                try:
                    report = Supervisor(self.settings).run_pipeline()
                    rec = report.get('agents', {}).get('recommendation', {})
                    ok = bool(report.get('ok') and not report.get('degraded') and rec.get('ok') and rec.get('records', 0) > 0)
                    state.update(status='completed' if ok else 'retry_wait', last_ok=ok, last_degraded=bool(report.get('degraded')),
                                 last_error=None if ok else '流水线失败、降级或未生成有效推荐')
                    if ok:
                        state['completed_date'] = today
                        state.pop('next_retry_at', None)
                    else:
                        state['next_retry_at'] = (shanghai_now()+timedelta(seconds=self.retry_seconds)).isoformat()
                    report['scheduler'] = {**state, 'enabled': self.enabled, 'daily_run_time': f'{self.hour:02d}:{self.minute:02d}'}
                    atomic_write_json(self.settings.runtime / 'state' / 'health.json', report)
                except Exception as exc:
                    state.update(status='retry_wait', last_ok=False, last_error=str(exc),
                                 next_retry_at=(shanghai_now()+timedelta(seconds=self.retry_seconds)).isoformat())
                    console_event('scheduler_run_failed', error_type=type(exc).__name__, message=str(exc))
                atomic_write_json(self.path, state)
                console_event('scheduler_run_finished', **state)
                if state.get('last_ok'):
                    try:
                        from .dashboard_enrichment import start_sync
                        start_sync(self.settings)
                    except Exception as exc:
                        console_event('dashboard_sync_start_failed',message=str(exc))
                return state.get('last_ok', False)
        finally:
            self._running.release()
