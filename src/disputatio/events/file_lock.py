"""Эксклюзивная блокировка вокруг read-check-write одного файла состояния.

`atomic_write` (temp-file + `os.replace`) атомарен для ОДНОЙ записи и этого
достаточно, пока писатель решает, что писать, не глядя на прежнее
содержимое. Хранилище пайплайна смотрит: оно читает манифест, сверяет
append-only коллекции с прочитанным и только потом пишет (§4.2). Между
чтением и записью помещается целиком чужой `save` — и его добавление
исчезает под `os.replace` второго писателя, не нарушив ни одного guard'а:
каждый сверял снимок с состоянием, прочитанным им самим. Два `disp
pipeline resume` над одним пайплайном — не выдуманный сценарий, живой
runner зовёт `save` напрямую, и сериализации вокруг нет никакой.

**Почему блокировка, а не compare-and-swap.** CAS требует атомарного
«замени, если содержимое всё ещё то, что я прочитал»; файловая система
такой операции не даёт — `os.replace` перезаписывает безусловно. Собрать
CAS из версионных файлов значило бы сменить раскладку §4.1 (манифест
пайплайна называется `pipeline.json` и снимается анкером P9 по имени).
Сериализация read-check-write даёт ту же гарантию имеющимися средствами:
проигравший писатель перечитывает уже обновлённое состояние, и его снимок
отвергает существующий guard истории — запись отклоняется громко, а не
теряется молча.

**Блокируется отдельный файл, а не сам манифест.** Оба примитива (POSIX
`flock`, Windows `msvcrt.locking`) держат блокировку на открытом
дескрипторе, то есть на inode; `atomic_write` подменяет манифест
`os.replace`'ом, после чего заблокированный inode — уже не тот файл, что
лежит по пути, и два писателя разошлись бы по разным inode, каждый со
своей «эксклюзивной» блокировкой. Поэтому рядом заводится долгоживущий
`<имя>.lock`, который никто не переименовывает и не удаляет.

Мёртвых блокировок оба примитива не оставляют: ОС снимает их при закрытии
дескриптора, в том числе когда процесс умер, не дойдя до `finally`.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

#: Суффикс файла-блокировки рядом с защищаемым путём.
LOCK_SUFFIX: Final = ".lock"


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Держит эксклюзивную блокировку `path` на время работы блока.

    Ожидание — блокирующее: второй писатель дожидается первого, а не падает
    (упасть ему предстоит на guard'е истории, если его снимок собран поверх
    устаревшего состояния, — и это другой, содержательный отказ).

    Каталог `path` обязан существовать: файл блокировки создаётся рядом, и
    его отсутствие даёт `FileNotFoundError` — ровно то же предусловие
    ([REQ-002]) и то же исключение, что и у `atomic_write`.
    """
    lock_path = path.with_name(path.name + LOCK_SUFFIX)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _acquire(descriptor)
        try:
            yield
        finally:
            _release(descriptor)
    finally:
        os.close(descriptor)


def _acquire(descriptor: int) -> None:
    """Блокирующий захват — `flock(LOCK_EX)`, на Windows `msvcrt.locking`."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        # LK_LOCK повторяет попытку около десяти секунд и затем поднимает
        # OSError — на этой платформе ожидание не бесконечно by design.
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _release(descriptor: int) -> None:
    """Снятие блокировки; закрытие дескриптора сняло бы её и само."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
