"""Helpers for other modules."""

import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import time

import colorama
import nanotime
from ruamel.yaml import YAML
from shortuuid import uuid

logger = logging.getLogger(__name__)

LOCAL_CHUNK_SIZE = 2 ** 20  # 1 MB
LARGE_FILE_SIZE = 2 ** 30  # 1 GB
LARGE_DIR_SIZE = 100
TARGET_REGEX = re.compile(r"(?P<path>.*?)(:(?P<name>[^\\/:]*))??$")


def dos2unix(data):
    return data.replace(b"\r\n", b"\n")


def _fobj_md5(fobj, hash_md5, binary, progress_func=None):
    while True:
        data = fobj.read(LOCAL_CHUNK_SIZE)
        if not data:
            break

        if binary:
            chunk = data
        else:
            chunk = dos2unix(data)

        hash_md5.update(chunk)
        if progress_func:
            progress_func(len(data))


def file_md5(fname, tree=None):
    """ get the (md5 hexdigest, md5 digest) of a file """
    from dvc.progress import Tqdm
    from dvc.istextfile import istextfile

    if tree:
        exists_func = tree.exists
        stat_func = tree.stat
        open_func = tree.open
    else:
        exists_func = os.path.exists
        stat_func = os.stat
        open_func = open

    if exists_func(fname):
        hash_md5 = hashlib.md5()
        binary = not istextfile(fname, tree=tree)
        size = stat_func(fname).st_size
        no_progress_bar = True
        if size >= LARGE_FILE_SIZE:
            no_progress_bar = False
            msg = (
                "Computing md5 for a large file '{}'. This is only done once."
            )
            logger.info(msg.format(relpath(fname)))
        name = relpath(fname)

        with Tqdm(
            desc=name,
            disable=no_progress_bar,
            total=size,
            bytes=True,
            leave=False,
        ) as pbar:
            with open_func(fname, "rb") as fobj:
                _fobj_md5(fobj, hash_md5, binary, pbar.update)

        return (hash_md5.hexdigest(), hash_md5.digest())

    return (None, None)


def bytes_hash(byts, typ):
    hasher = getattr(hashlib, typ)()
    hasher.update(byts)
    return hasher.hexdigest()


def dict_filter(d, exclude=()):
    """
    Exclude specified keys from a nested dict
    """

    if isinstance(d, list):
        return [dict_filter(e, exclude) for e in d]

    if isinstance(d, dict):
        return {
            k: dict_filter(v, exclude)
            for k, v in d.items()
            if k not in exclude
        }

    return d


def dict_hash(d, typ, exclude=()):
    filtered = dict_filter(d, exclude)
    byts = json.dumps(filtered, sort_keys=True).encode("utf-8")
    return bytes_hash(byts, typ)


def dict_md5(d, **kwargs):
    return dict_hash(d, "md5", **kwargs)


def dict_sha256(d, **kwargs):
    return dict_hash(d, "sha256", **kwargs)


def _split(list_to_split, chunk_size):
    return [
        list_to_split[i : i + chunk_size]
        for i in range(0, len(list_to_split), chunk_size)
    ]


def _to_chunks_by_chunks_number(list_to_split, num_chunks):
    chunk_size = int(math.ceil(float(len(list_to_split)) / num_chunks))

    if len(list_to_split) == 1:
        return [list_to_split]

    if chunk_size == 0:
        chunk_size = 1

    return _split(list_to_split, chunk_size)


def to_chunks(list_to_split, num_chunks=None, chunk_size=None):
    if (num_chunks and chunk_size) or (not num_chunks and not chunk_size):
        raise ValueError(
            "Only one among `num_chunks` or `chunk_size` must be defined."
        )
    if chunk_size:
        return _split(list_to_split, chunk_size)
    return _to_chunks_by_chunks_number(list_to_split, num_chunks)


# NOTE: Check if we are in a bundle
# https://pythonhosted.org/PyInstaller/runtime-information.html
def is_binary():
    return getattr(sys, "frozen", False)


def fix_env(env=None):
    """Fix env variables modified by PyInstaller [1] and pyenv [2].
    [1] http://pyinstaller.readthedocs.io/en/stable/runtime-information.html
    [2] https://github.com/pyenv/pyenv/issues/985
    """
    if env is None:
        env = os.environ.copy()
    else:
        env = env.copy()

    if is_binary():
        lp_key = "LD_LIBRARY_PATH"
        lp_orig = env.get(lp_key + "_ORIG", None)
        if lp_orig is not None:
            env[lp_key] = lp_orig
        else:
            env.pop(lp_key, None)

    # Unlike PyInstaller, pyenv doesn't leave backups of original env vars
    # when it modifies them. If we look into the shim, pyenv and pyenv-exec,
    # we can figure out that the PATH is modified like this:
    #
    #     PATH=$PYENV_BIN_PATH:${bin_path}:${plugin_bin}:$PATH
    #
    # where
    #
    #     PYENV_BIN_PATH - might not start with $PYENV_ROOT if we are running
    #         `system` version of the command, see pyenv-exec source code.
    #     bin_path - might not start with $PYENV_ROOT as it runs realpath on
    #         it, but always has `libexec` part in it, see pyenv source code.
    #     plugin_bin - might contain more than 1 entry, which start with
    #         $PYENV_ROOT, see pyenv source code.
    #
    # Also, we know that whenever pyenv is running, it exports these env vars:
    #
    #     PYENV_DIR
    #     PYENV_HOOK_PATH
    #     PYENV_VERSION
    #     PYENV_ROOT
    #
    # So having this, we can make a rightful assumption about what parts of the
    # PATH we need to remove in order to get the original PATH.
    path = env.get("PATH", "")
    parts = path.split(":")
    bin_path = parts[1] if len(parts) > 2 else ""
    pyenv_dir = env.get("PYENV_DIR")
    pyenv_hook_path = env.get("PYENV_HOOK_PATH")
    pyenv_version = env.get("PYENV_VERSION")
    pyenv_root = env.get("PYENV_ROOT")

    env_matches = all([pyenv_dir, pyenv_hook_path, pyenv_version, pyenv_root])

    bin_path_matches = os.path.basename(bin_path) == "libexec"

    # NOTE: we don't support pyenv-win
    if os.name != "nt" and env_matches and bin_path_matches:
        # removing PYENV_BIN_PATH and bin_path
        parts = parts[2:]

        if parts:
            # removing plugin_bin from the left
            plugin_bin = os.path.join(pyenv_root, "plugins")
            while parts[0].startswith(plugin_bin):
                del parts[0]

        env["PATH"] = ":".join(parts)

    return env


def tmp_fname(fname):
    """ Temporary name for a partial download """
    return os.fspath(fname) + "." + uuid() + ".tmp"


def current_timestamp():
    return int(nanotime.timestamp(time.time()))


def from_yaml_string(s):
    return YAML().load(io.StringIO(s))


def to_yaml_string(data):
    stream = io.StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(data, stream)
    return stream.getvalue()


def colorize(message, color=None):
    """Returns a message in a specified color."""
    if not color:
        return message

    colors = {
        "green": colorama.Fore.GREEN,
        "yellow": colorama.Fore.YELLOW,
        "blue": colorama.Fore.BLUE,
        "red": colorama.Fore.RED,
    }

    return "{color}{message}{nc}".format(
        color=colors.get(color, ""), message=message, nc=colorama.Fore.RESET
    )


def boxify(message, border_color=None):
    """Put a message inside a box.

    Args:
        message (unicode): message to decorate.
        border_color (unicode): name of the color to outline the box with.
    """
    lines = message.split("\n")
    max_width = max(_visual_width(line) for line in lines)

    padding_horizontal = 5
    padding_vertical = 1

    box_size_horizontal = max_width + (padding_horizontal * 2)

    chars = {"corner": "+", "horizontal": "-", "vertical": "|", "empty": " "}

    margin = "{corner}{line}{corner}\n".format(
        corner=chars["corner"], line=chars["horizontal"] * box_size_horizontal
    )

    padding_lines = [
        "{border}{space}{border}\n".format(
            border=colorize(chars["vertical"], color=border_color),
            space=chars["empty"] * box_size_horizontal,
        )
        * padding_vertical
    ]

    content_lines = [
        "{border}{space}{content}{space}{border}\n".format(
            border=colorize(chars["vertical"], color=border_color),
            space=chars["empty"] * padding_horizontal,
            content=_visual_center(line, max_width),
        )
        for line in lines
    ]

    box_str = "{margin}{padding}{content}{padding}{margin}".format(
        margin=colorize(margin, color=border_color),
        padding="".join(padding_lines),
        content="".join(content_lines),
    )

    return box_str


def _visual_width(line):
    """Get the the number of columns required to display a string"""

    return len(re.sub(colorama.ansitowin32.AnsiToWin32.ANSI_CSI_RE, "", line))


def _visual_center(line, width):
    """Center align string according to it's visual width"""

    spaces = max(width - _visual_width(line), 0)
    left_padding = int(spaces / 2)
    right_padding = spaces - left_padding

    return (left_padding * " ") + line + (right_padding * " ")


def relpath(path, start=os.curdir):
    path = os.fspath(path)
    start = os.path.abspath(os.fspath(start))

    # Windows path on different drive than curdir doesn't have relpath
    if os.name == "nt" and not os.path.commonprefix(
        [start, os.path.abspath(path)]
    ):
        return path
    return os.path.relpath(path, start)


def env2bool(var, undefined=False):
    """
    undefined: return value if env var is unset
    """
    var = os.getenv(var, None)
    if var is None:
        return undefined
    return bool(re.search("1|y|yes|true", var, flags=re.I))


def resolve_output(inp, out):
    from urllib.parse import urlparse

    name = os.path.basename(os.path.normpath(urlparse(inp).path))
    if not out:
        return name
    if os.path.isdir(out):
        return os.path.join(out, name)
    return out


def resolve_paths(repo, out):
    from ..dvcfile import DVC_FILE_SUFFIX
    from ..path_info import PathInfo
    from .fs import contains_symlink_up_to

    abspath = os.path.abspath(out)
    dirname = os.path.dirname(abspath)
    base = os.path.basename(os.path.normpath(out))

    # NOTE: `out` might not exist yet, so using `dirname`(aka `wdir`) to check
    # if it is a local path.
    if (
        os.path.exists(dirname)  # out might not exist yet, so
        and PathInfo(abspath).isin_or_eq(repo.root_dir)
        and not contains_symlink_up_to(abspath, repo.root_dir)
    ):
        wdir = dirname
        out = base
    else:
        wdir = os.getcwd()

    path = os.path.join(wdir, base + DVC_FILE_SUFFIX)

    return (path, wdir, out)


def format_link(link):
    return "<{blue}{link}{nc}>".format(
        blue=colorama.Fore.CYAN, link=link, nc=colorama.Fore.RESET
    )


def parse_target(target, default=None):
    from dvc.dvcfile import PIPELINE_FILE, PIPELINE_LOCK, is_valid_filename
    from dvc.exceptions import DvcException

    if not target:
        return None, None

    match = TARGET_REGEX.match(target)
    if not match:
        return target, None

    path, name = (
        match.group("path"),
        match.group("name"),
    )
    if path:
        if os.path.basename(path) == PIPELINE_LOCK:
            raise DvcException(
                "Did you mean: `{}`?".format(
                    target.replace(".lock", ".yaml", 1)
                )
            )
        if not name:
            ret = (target, None)
            return ret if is_valid_filename(target) else ret[::-1]

    if not path:
        path = default or PIPELINE_FILE
        logger.debug("Assuming file to be '%s'", path)

    return path, name
