from enum import Enum
from itertools import islice
from pathlib import Path
import asyncio
import importlib.util
import os
import subprocess
import sys
import logging
import logging.handlers
import statistics
import zipfile
from typing import Iterable, Optional, Sequence, TypeVar
from PyQt5.QtCore import QStandardPaths

T = TypeVar("T")

is_windows = sys.platform.startswith("win")
is_macos = sys.platform == "darwin"
is_linux = not is_windows and not is_macos


def _get_user_data_dir():
    if importlib.util.find_spec("krita") is None:
        dir = Path(__file__).parent.parent / ".appdata"
        dir.mkdir(exist_ok=True)
        return dir
    try:
        dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
        if dir.exists() and "krita" in dir.name.lower():
            dir = dir / "ai_diffusion"
        else:
            dir = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation))
            dir = dir / "krita-ai-diffusion"
        dir.mkdir(exist_ok=True)
        return dir
    except Exception as e:
        return Path(__file__).parent


user_data_dir = _get_user_data_dir()


def _get_log_dir():
    dir = user_data_dir / "logs"
    dir.mkdir(exist_ok=True)

    legacy_dir = Path(__file__).parent / ".logs"
    try:  # Move logs from old location (v1.14 and earlier)
        if legacy_dir.exists():
            for file in legacy_dir.iterdir():
                file.rename(dir / file.name)
            legacy_dir.rmdir()
    except Exception:
        print(f"Failed to move logs from {legacy_dir} to {dir}")

    return dir


def create_logger(name: str, path: Path):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if os.environ.get("AI_DIFFUSION_ENV") == "WORKER":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.RotatingFileHandler(
            path, encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=4
        )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


log_dir = _get_log_dir()
client_logger = create_logger("krita.ai_diffusion.client", log_dir / "client.log")
server_logger = create_logger("krita.ai_diffusion.server", log_dir / "server.log")


def log_error(error: Exception):
    if isinstance(error, AssertionError):
        message = f"Error: Internal assertion failed [{error}]"
    else:
        message = f"Error: {error}"
    client_logger.exception(message)
    return message


def ensure(value: Optional[T]) -> T:
    assert value is not None
    return value


def batched(iterable, n):
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


def median_or_zero(values: Iterable[float]) -> float:
    try:
        return statistics.median(values)
    except statistics.StatisticsError:
        return 0


def encode_json(obj):
    if isinstance(obj, Enum):
        return obj.name
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def sanitize_prompt(prompt: str):
    if prompt == "":
        return "no prompt"
    prompt = prompt[:40]
    return "".join(c for c in prompt if c.isalnum() or c in " _-")


def find_unused_path(path: Path):
    """Finds an unused path by appending a number to the filename"""
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    i = 1
    while (new_path := path.with_name(f"{stem}-{i}{ext}")).exists():
        i += 1
    return new_path


def get_path_dict(paths: Sequence[str]) -> dict:
    """Builds a tree like structure out of a list of paths. The leaf nodes point to the original
    path string. It's important the string remains unchanged, see #307"""

    def _recurse(dic: dict, chain: tuple[str, ...] | list[str], full_path: str):
        if len(chain) == 0:
            return
        if len(chain) == 1:
            dic[chain[0]] = full_path
            return
        key, *new_chain = chain
        _recurse(dic.setdefault(key, {}), new_chain, full_path)
        return

    new_path_dict = {}
    for path in paths:
        _recurse(new_path_dict, Path(path).parts, path)
    return new_path_dict


if is_linux:
    import signal
    import ctypes

    libc = ctypes.CDLL("libc.so.6")

    def set_pdeathsig():
        return libc.prctl(1, signal.SIGTERM)


async def create_process(
    program: str | Path,
    *args: str,
    cwd: Path | None = None,
    additional_env: dict | None = None,
    pipe_stderr=False,
):
    platform_args = {}
    if is_windows:
        platform_args["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore
    if is_linux:
        platform_args["preexec_fn"] = set_pdeathsig

    env = os.environ.copy()
    if additional_env:
        env.update(additional_env)
    if "PYTHONPATH" in env:
        del env["PYTHONPATH"]  # Krita adds its own python path, which can cause conflicts

    out = asyncio.subprocess.PIPE
    err = asyncio.subprocess.PIPE if pipe_stderr else asyncio.subprocess.STDOUT

    p = await asyncio.create_subprocess_exec(
        program, *args, cwd=cwd, stdout=out, stderr=err, env=env, **platform_args
    )
    if is_windows:
        from . import win32

        win32.attach_process_to_job(p.pid)
    return p


class LongPathZipFile(zipfile.ZipFile):
    # zipfile.ZipFile does not support long paths (260+?) on Windows
    # for latest python, changing cwd and using relative paths helps, but not for python in Krita 5.2
    def _extract_member(self, member, targetpath, pwd):
        # Prepend \\?\ to targetpath to bypass MAX_PATH limit
        targetpath = os.path.abspath(targetpath)
        if targetpath.startswith("\\\\"):
            targetpath = "\\\\?\\UNC\\" + targetpath[2:]
        else:
            targetpath = "\\\\?\\" + targetpath
        return super()._extract_member(member, targetpath, pwd)  # type: ignore


ZipFile = LongPathZipFile if is_windows else zipfile.ZipFile
