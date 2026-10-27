"""Single-host process exclusion, independent of application scheduling."""
from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            # Windows 按字节范围锁定，空文件先写占位字节，再锁定第一个字节。
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        acquired = False
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
            # 非阻塞获取：未取得锁也返回给调用方，由业务决定跳过或报待处理。
            # 锁的所有权来自打开的句柄，不以磁盘上是否存在 .lock 文件判断。
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

