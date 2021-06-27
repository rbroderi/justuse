"""
A self-documenting, explicit, functional way to import modules in Python with advanced features.

Goals/Features:
- version check on the spot, potential version conflicts become obvious (DONE)
- securely load standalone-modules from online sources (DONE)
- safely auto-reloading of local modules on edit (preliminary DONE - works in jupyter)
- pass module-level globals into the importing context (DONE)
- return optional fallback-default object/module if import failed (DONE)
- aspect-oriented decorators for everything callable on import (DONE)
- securely auto-install packages (TODO)
- support P2P package distribution (TODO)
- unwrap aspect-decorators on demand (TODO)
- easy introspection via internal dependency graph (TODO)
- relative imports on online-sources via URL-aliases (TODO)
- module-level variable placeholders/guards aka "module-properties" (TODO)

Non-Goal:
Completely replace the import statement.

Notes: 
pathlib.Path and yarl.URL can both be accessed as aliases via use.Path and use.URL
inspect.isfunction, .ismethod and .isclass also can be accessed via their aliases use.isfunction, use.ismethod and use.isclass

Examples:
>>> import use

# equivalent to `import numpy as np` with explicit version check
>>> np = use("numpy", version="1.1.1")
>>> np.version == "1.1.1"
True

# equivalent to `from pprint import pprint; pprint(dictionary)` but without assigning 
# pprint to a global variable, thus no namespace pollution
>>> use("pprint").pprint([1,2,3])
[1,2,3]
# equivalent to sys.path manipulation, then `import tools` with a reload(tools) every second
>>> tools = use(use.Path("/media/sf_Dropbox/code/tools.py"), reloading=True)

# it is possible to import standalone modules from online sources
# with immediate sha1-hash-verificiation before execution of the code like
>>> utils = use(use.URL("https://raw.githubusercontent.com/PIA-Group/BioSPPy/7696d682dc3aafc898cd9161f946ea87db4fed7f/biosppy/utils.py"),
                    hash_value="95f98f25ef8cfa0102642ea5babbe6dde3e3a19d411db9164af53a9b4cdcccd8")

# to auto-install a certain version (within a virtual env and pip in secure hash-check mode) of a package you can do
>>> np = use("numpy", version="1.1.1", auto_install=True, hash_value=["9879de676"])

File-Hashing inspired by 
- https://github.com/kalafut/py-imohash
- https://github.com/fmoo/python-varint/blob/master/varint.py

:author: use-github@anselm.kiefner.de (Anselm Kiefner)
:license: MIT
"""

import asyncio
import atexit
import codecs
import configparser
import hashlib
import importlib
import inspect
import json
import linecache
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections import defaultdict, namedtuple
from enum import Enum
from functools import singledispatch, update_wrapper
from importlib import metadata
from pathlib import Path
from types import ModuleType
from warnings import warn

import mmh3
import requests
from packaging.version import parse
from yarl import URL

__version__ = "0.2.7"

_reloaders = {}  # ProxyModule:Reloader
_aspects = {} 
_using = {}

mode = Enum("Mode", "fastfail")

# sometimes all you need is a sledge hammer..
def signal_handler(sig, frame):
    for reloader in _reloaders.values():
        reloader.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def varint_encode(number):
    """Pack `number` into varint bytes"""
    buf = b''
    while True:
        towrite = number & 0x7f
        number >>= 7
        if number:
            buf += bytes((towrite | 0x80,))
        else:
            buf += bytes((towrite,))
            break
    return buf

def hashfileobject(code, sample_threshhold=128 * 1024, sample_size=16 * 1024):
    size = len(code)
    hash_tmp = mmh3.hash_bytes(code)
    hash_ = hash_tmp[7::-1] + hash_tmp[16:7:-1]
    enc_size = varint_encode(size)
    return enc_size + hash_[len(enc_size):]

def securehash_file(file, hash_algo):
    BUF_SIZE = 65536
    if hash_algo is Use.mode.sha256:
        file_hash = hashlib.sha256()
    while True:
        data = file.read(BUF_SIZE)
        if not data:
            break
        file_hash.update(data)
    return file_hash.hexdigest()

def methdispatch(func):
    dispatcher = singledispatch(func)
    def wrapper(*args, **kw):
        return dispatcher.dispatch(args[1].__class__)(*args, **kw)
    wrapper.register = dispatcher.register
    update_wrapper(wrapper, func)
    return wrapper

def build_mod(*, name:str, 
                code:bytes, 
                initial_globals:dict, 
                module_path:str, 
                aspectize:dict, 
                default=mode.fastfail) -> ModuleType:
    mod = ModuleType(name)
    print(2, 5, code, type(code))
    mod.__dict__.update(initial_globals or {})
    mod.__file__ = module_path
    code_text = codecs.decode(code)
    # module file "<", ">" chars are specially handled by inspect
    linecache.cache[f"<{name}>"] = (
    len(code), # size of source code
    None, # last modified time; None means there is no physical file
    [*map( # a list of lines, including trailing newline on each
        lambda ln: ln+"\x0a",
        code_text.splitlines())
    ],
    mod.__file__, # file name, e.g. "<mymodule>" or the actual path to the file
    )
    # not catching this causes the most irritating bugs ever!
    try:
        exec(compile(code, f"<{name}>", "exec"), mod.__dict__)
    except: # reraise anything without handling - clean and simple.
        raise
    for (check, pattern), decorator in aspectize.items():
        apply_aspect(mod, check, pattern, decorator)
    return mod

def fail_or_default(default, exception, msg):
    if default is not Use.mode.fastfail:
        return default
    else:
        raise exception(msg)

def apply_aspect(mod:ModuleType, check:callable, pattern:str, decorator:callable):
    """Apply the aspect as a side-effect, no copy is created."""
    # TODO: recursion?
    parent = mod
    for name, obj in parent.__dict__.items():
        if check(obj) and re.match(pattern, obj.__qualname__):
            # TODO: logging?
            parent.__dict__[obj.__name__] = decorator(obj)
    return mod

class SurrogateModule(ModuleType):
    def __init__(self, *, name, path, mod, initial_globals, aspectize):
        self.__implementation = mod
        self.__stopped = False

        def __reload_threaded():
            last_filehash = None
            while not self.__stopped:
                with open(path, "rb") as file:
                    code = file.read()
                current_filehash = hashfileobject(code)
                if current_filehash != last_filehash:
                    try:
                        mod = build_mod(name=name, 
                                        code=code, 
                                        initial_globals=initial_globals,
                                        module_path=path.resolve(),
                                        aspectize=aspectize)
                        self.__implementation = mod
                    except:
                        print(traceback.format_exc())
                last_filehash = current_filehash
                time.sleep(1)

        async def __reload_async():
            last_filehash = None
            while not self.__stopped:
                with open(path, "rb") as file:
                    code = file.read()
                current_filehash = hashfileobject(code)
                if current_filehash != last_filehash:
                    try:
                        mod = build_mod(name=name, 
                                        code=code, 
                                        initial_globals=initial_globals,
                                        module_path=path.resolve(),
                                        aspectize=aspectize)
                        self.__implementation = mod
                    except:
                        print(traceback.format_exc())
                last_filehash = current_filehash
                await asyncio.sleep(1)
        try:
            # this looks like a hack, but isn't one - 
            # jupyter is running an async loop internally, which works better async than threaded!
            loop = asyncio.get_running_loop()
            loop.create_task(__reload_async())
        except RuntimeError:
            atexit.register(self.__stop)
            self.__thread = threading.Thread(target=__reload_threaded, name=f"reloader__{name}")
            self.__thread.start()

    def __del__(self):
        self.__stopped = True

    def __stop(self):
        self.__stopped = True

    def __getattribute__(self, name):
        if name in ( 
                    "_SurrogateModule__implementation",
                    "_SurrogateModule__stopped",
                    "_SurrogateModule__thread",
                    "_SurrogateModule__stop",
                    ):
            return object.__getattribute__(self, name)
        else:
            return getattr(self.__implementation, name)
    
    def __setattr__(self, name, value):
        if name in (
                    "_SurrogateModule__implementation",
                    "_SurrogateModule__stopped",
                    "_SurrogateModule__thread",
                    "_SurrogateModule__stop",
                    ):
            object.__setattr__(self, name, value)
        else:
            setattr(self.__implementation, name, value)


class ProxyModule(ModuleType):
    def __init__(self, mod):
        self.__implementation = mod
        self.__condition = threading.RLock()

    def __getattribute__(self, name):
        if name in ( 
                    "_ProxyModule__implementation",
                    "_ProxyModule__condition",
                    ""
                    ):
            return object.__getattribute__(self, name)
        with self.__condition:
            return getattr(self.__implementation, name)
    
    def __setattr__(self, name, value):
        if name in (
                    "_ProxyModule__implementation",
                    "_ProxyModule__condition",
                    ):
            object.__setattr__(self, name, value)
            return
        with self.__condition:
            setattr(self.__implementation, name, value)

class ModuleReloader:
    def __init__(self, *, proxy, name, path, initial_globals, aspectize):
        self.proxy = proxy
        self.name = name
        self.path = path
        self.initial_globals = initial_globals
        self.aspectize = aspectize
        self._condition = threading.RLock()
        self._stopped = True
        self._thread = None
        
    def start(self):
        assert not (self._thread is not None and not self._thread.is_alive()), "Can't start another reloader thread while one is already running."
        self._stopped = False
        atexit.register(self.stop)
        self._thread = threading.Thread(target=self.run_threaded, name=f"reloader__{self.name}")
        self._thread.start()
    
    def run_threaded(self):
        last_filehash = None
        while not self._stopped:
            with self._condition:
                with open(self.path, "rb") as file:
                    code = file.read()
                current_filehash = hashfileobject(code)
                if current_filehash != last_filehash:
                    try:
                        mod = build_mod(name=self.name, 
                                        code=code, 
                                        initial_globals=self.initial_globals,
                                        module_path=self.path,
                                        aspectize=self.aspectize)
                        self.proxy._ProxyModule__implementation = mod
                    except:
                        print(traceback.format_exc())
                last_filehash = current_filehash
            time.sleep(1)
    
    def stop(self):
        self._stopped = True
    
    def __del__(self):
        self.stop()
        atexit.unregister(self.stop)
class Use:
    # lift module-level stuff up
    __doc__ = __doc__
    __version__ = __version__  # otherwise setup.py can't find it
    
    # attempt at fix for #23 doesn't work..
    __path__ = str(Path(__file__).resolve().parent)
    Path = Path
    URL = URL
    class Hash(Enum):
        sha256 = hashlib.sha256
        
    ModInUse = namedtuple("ModInUse", "name mod path spec frame")

    mode = mode
    
    # ALIASES
    isfunction = inspect.isfunction
    ismethod = inspect.ismethod
    isclass = inspect.isclass   
    class VersionWarning(Warning):
        pass

    class NotReloadableWarning(Warning):
        pass

    class NoValidationWarning(Warning):
        pass

    class AmbiguityWarning(Warning):
        pass

    class ModuleNotFoundError(ImportError):
        pass

    class UnexpectedHash(ImportError):
        pass

    class AutoInstallationError(ImportError):
        pass
    
    def __init__(self):
        self._registry = {"version":"0.0.1", 
                        "distributions": defaultdict(lambda: list())
                        }
        self._using = _using
        self._aspects = _aspects
        self._reloaders = _reloaders
        for d in metadata.distributions():
            self._registry["distributions"][d.metadata["Name"]].append({"version": d.version, "path": d})

        self.home = Path.home() / ".justuse-python"
        self.home.mkdir(mode=0o755, exist_ok=True)
        (self.home / "packages").mkdir(mode=0o755, exist_ok=True)
        (self.home / "registry.json").touch(mode=0o644, exist_ok=True)
        (self.home / "config.ini").touch(mode=0o644, exist_ok=True)
        (self.home / "usage.log").touch(mode=0o644, exist_ok=True)
        
        self.config = configparser.ConfigParser()
        with open(self.home / "config.ini") as file:
            self.config.read(file)
        
        try:
            with open(self.home / "registry.json") as file:
                if len(file.read()) == 0:
                    raise ValueError  # short-circuit the pending JSONDecodeError
                self._registry.update(json.load(file))
        except ValueError:
            pass

    def set_mod(self, *, name, mod, spec, path, frame):
        """Helper to get the order right."""
        self._using[name] = Use.ModInUse(name, mod, path, spec, frame)

    @methdispatch
    def __call__(self, thing, /, *args, **kwargs):
        raise NotImplementedError(f"Only pathlib.Path, yarl.URL and str are valid sources of things to import, but got {type(thing)}.")

    @__call__.register(URL)
    def _use_url(
                self, 
                url:URL, 
                /,*,
                hash_algo:Hash=Hash.sha256, 
                hash_value:str=None, 
                initial_globals:dict=None, 
                as_import:str=None,
                default=mode.fastfail,
                aspectize:dict=None,
                path_to_url:dict=None,
                import_to_use: dict=None,
                ) -> ModuleType:
        exc = None
        
        assert hash_algo in Use.Hash, f"{hash_algo} is not a valid hashing algorithm!"
        
        aspectize = aspectize or {}
        response = requests.get(url)
        if response.status_code != 200:
            raise ModuleNotFoundError(f"Could not load {url} from the interwebs, got a {response.status_code} error.")
        this_hash = hash_algo.value(response.content).hexdigest()
        if hash_value:
            if this_hash != hash_value:
                return fail_or_default(default, Use.UnexpectedHash, f"{this_hash} does not match the expected hash {hash_value} - aborting!")
        else:
            warn(f"""Attempting to import from the interwebs with no validation whatsoever! 
To safely reproduce: use(use.URL('{url}'), hash_algo=use.{hash_algo}, hash_value='{this_hash}')""", 
                Use.NoValidationWarning)
        name = url.name
        
        try:
            mod = build_mod(name=name, 
                            code=response.content, 
                            module_path=url.path,
                            initial_globals=initial_globals, 
                            aspectize=aspectize)
        except:
            exc = traceback.format_exc()
        if exc:
            return fail_or_default(default, ImportError, exc)
        
        self.set_mod(name=name, mod=mod, spec=None, path=url, frame=inspect.getframeinfo(inspect.currentframe()))
        if as_import:
            assert isinstance(as_import, str), f"as_import must be the name (as str) of the module as which it should be imported, got {as_import} ({type(as_import)}) instead."
            assert as_import.isidentifier(), f"as_import must be a valid identifier."
            sys.modules[as_import] = mod
        return mod

    @__call__.register(Path)
    def _use_path(
                self, 
                path:Path, 
                /,*,
                reloading:bool=False,
                initial_globals:dict=None, 
                as_import:str=None,
                default=mode.fastfail,
                aspectize:dict=None,
                path_to_url:dict=None,
                import_to_use: dict=None,
                ) -> ModuleType: 
        aspectize = aspectize or {}
        initial_globals = initial_globals or {}
        exc = None
        mod = None
        
        if path.is_dir():
            return fail_or_default(default, ImportError, f"Can't import directory {path}")
        
        original_cwd = Path.cwd()
        if not path.is_absolute():
            source_dir = getattr(self._using.get(inspect.currentframe().f_back.f_back.f_code.co_filename), "path", None)
            
            # calling from another use()d module
            if source_dir:
                # if calling from an actual file, we take that as starting point
                if source_dir.exists():
                    os.chdir(source_dir.parent)
                    source_dir = source_dir.parent
                else:
                    return fail_or_default(default, NotImplementedError, "Can't determine a relative path from a virtual file.")
            # there are a number of ways to call use() from a non-use() starting point
            else:
                # let's first check if we are running in jupyter
                jupyter = "ipykernel" in sys.modules
                # we're in jupyter, we use the CWD as set in the notebook
                if jupyter:
                    source_dir = original_cwd
                else:
                    # let's see where we started
                    main_mod = __import__("__main__")
                    # if we're calling from a script file e.g. `python3 my/script.py` like pytest unittest
                    if hasattr(main_mod, "__file__"):
                        source_dir = Path(inspect.currentframe().f_back.f_back.f_code.co_filename).resolve().parent
                    else:
                        # interactive startup - use current directory
                        source_dir = original_cwd
        path = source_dir.joinpath(path).resolve()
        if not path.exists():
            os.chdir(original_cwd)
            return fail_or_default(default, ModuleNotFoundError, f"Sure '{path}' exists?")
        os.chdir(path.parent)
        name = path.stem
        if reloading:
            try:
                with open(path, "rb") as file:
                    code = file.read()
                # initial instance, if this doesn't work, just throw the towel
                mod = build_mod(name=name, 
                                code=code, 
                                initial_globals=initial_globals, 
                                module_path=path.resolve(), 
                                aspectize=aspectize
                                )
            except:
                exc = traceback.format_exc()
            if exc:
                return fail_or_default(default, ImportError, exc)
            
            threaded = False
            try:
                # this looks like a hack, but isn't one - 
                # jupyter is running an async loop internally, which works better async than threaded!
                asyncio.get_running_loop()
                
                # Old, working implementation
                mod = SurrogateModule(
                    name=name, 
                    path=path,
                    mod=mod,
                    initial_globals=initial_globals, 
                    aspectize=aspectize
                    )
            # we're dealing with non-async code, we need threading
            # new experimental implementation
            except RuntimeError:
                # can't have the code inside the handler because of "during handling of X, another exception Y happened"
                threaded = True
                
            if threaded:
                mod = ProxyModule(mod)
                reloader = ModuleReloader(
                                        proxy=mod,
                                        name=name, 
                                        path=path, 
                                        initial_globals=initial_globals, 
                                        aspectize=aspectize,
                                        )
                _reloaders[mod] = reloader
                reloader.start()
        
            if not all(inspect.isfunction(value) for key, value in mod.__dict__.items() 
                        if key not in initial_globals.keys() and not key.startswith("__")):
                warn(
                    f"Beware {name} also contains non-function objects, it may not be safe to reload!",
                    Use.NotReloadableWarning,
                )
        else:
            with open(path, "rb") as file:
                code = file.read()
            # the path needs to be set before attempting to load the new module - recursion confusing ftw!
            self.set_mod(name=f"<{name}>", mod=mod, path=path, spec=None, frame=inspect.getframeinfo(inspect.currentframe()))
            try:
                mod = build_mod(name=name, 
                                code=code, 
                                initial_globals=initial_globals, 
                                module_path=path, 
                                aspectize=aspectize)
            except:
                del self._using[f"<{name}>"]
                exc = traceback.format_exc()
        # let's not confuse the user and restore the cwd to the original in any case
        os.chdir(original_cwd)
        if exc:
            return fail_or_default(default, ImportError, exc)
        if as_import:
            assert isinstance(as_import, str), f"as_import must be the name (as str) of the module as which it should be imported, got {as_import} ({type(as_import)}) instead."
            sys.modules[as_import] = mod
        self.set_mod(name=f"<{name}>", mod=mod, path=path, spec=None, frame=inspect.getframeinfo(inspect.currentframe()))
        return mod

    @__call__.register(str)
    def _use_str(
                self,
                name:str,
                /,*,
                version:str="", 
                initial_globals:dict=None, 
                auto_install:bool=False, 
                hash_algo:str=Hash.sha256, 
                hash_value:str=None,
                default=mode.fastfail,
                aspectize=None,
                path_to_url:dict=None,
                import_to_use: dict=None,
                ) -> ModuleType:
        initial_globals = initial_globals or {}
        aspectize = aspectize or {}
        target_version = parse(str(version)) if version else None  # the empty str parses as a truey LegacyVersion - WTF
        exc: str = None
        mod: ModuleType = None
        
        if initial_globals or import_to_use or path_to_url:
            raise NotImplementedError("If you require this functionality, please report it on https://github.com/amogorkon/justuse/issues so we can work out the specifics together.")

        # The "try and guess" behaviour is due to how classical imports work, 
        # which is inherently ambiguous, but can't really be avoided for packages.

        # let's first see if the user might mean something else entirely
        if any(Path(".").glob(f"{name}.py")):
            warn(f"Attempting to load the package '{name}', if you rather want to use the local module: use(use.Path('{name}.py'))", 
                Use.AmbiguityWarning)

        if name in self._using:
            spec = self._using[name].spec
        else:
            spec = importlib.util.find_spec(name)
        
        if spec:
            # let's check if it's a builtin
            builtin = False
            try:
                x = metadata.PathDistribution.from_name(name)
            except metadata.PackageNotFoundError:  # indeed builtin!
                builtin = True
            if builtin:
                try:
                    mod = spec.loader.create_module(spec)
                    spec.loader.exec_module(mod)  # ! => cache
                    for (check, pattern), decorator in aspectize.items():
                        apply_aspect(mod, check, pattern, decorator)
                    self.set_mod(name=name, mod=mod, spec=spec, path=None, frame=inspect.getframeinfo(inspect.currentframe()))
                    return mod
                except:
                    exc = traceback.format_exc()
                if exc:
                    return fail_or_default(default, ImportError, exc)

            # it seems to be installed in some way, for instance via pip
            if not auto_install:
                try:
                    # feels like cheating, doesn't it
                    mod = importlib.import_module(name)  # ! => cache
                    for (check, pattern), decorator in aspectize.items():
                        apply_aspect(mod, check, pattern, decorator)
                    self.set_mod(name=name, mod=mod, spec=spec, path=None, frame=inspect.getframeinfo(inspect.currentframe()))
                    warn(f"Classically imported '{name}'. To pin this version use('{name}', version='{metadata.version(name)}')", Use.AmbiguityWarning)
                except:
                    exc = traceback.format_exc()
                if exc:
                    return fail_or_default(default, ImportError, exc)
            
                # we only enforce versions with auto-install
                if target_version:
                    # pure despair :(
                    for check in [
                        "metadata.distribution(name).version",
                        "mod.version",
                        "mod.version()",
                        "mod.__version__"]:
                        try:
                            check_value = eval(check)
                            if isinstance(check_value, str):
                                this_version = parse(check_value)
                                if target_version != this_version:
                                    warn(
                                        f"{name} is expected to be version {target_version} ,  but got {this_version} instead",
                                        Use.VersionWarning,
                                    )
                                    break
                        except:
                            pass
                    else:
                        print(f"Cannot determine version for module {name}, continueing.")
            # spec & auto-install
            else:
                if (metadata.version(name) == target_version) or not(version):
                    if not (version):
                        warn(Use.AmbiguityWarning("No version was provided, even though auto_install was specified! Trying to load classically installed package instead."))
                    try:
                        mod = importlib.import_module(name)  # ! => cache
                        for (check, pattern), decorator in aspectize.items():
                            apply_aspect(mod, check, pattern, decorator)
                        self.set_mod(name=name, mod=mod, spec=spec, path=None, frame=inspect.getframeinfo(inspect.currentframe()))
                        warn(f"Classically imported '{name}'. To pin this version use('{name}', version='{metadata.version(name)}')", Use.AmbiguityWarning)
                    except:
                        exc = traceback.format_exc()
                    if exc:
                        return fail_or_default(default, ImportError, exc)
                # wrong version => wrong spec
                if metadata.version(name) != target_version:
                    spec = None
        # no spec
        else:
            if not auto_install:
                return fail_or_default(default, ImportError, f"Could not find any installed package '{name}' and auto_install was not requested.")
            
            # the whole auto-install shebang
            if target_version and not hash_value:
                raise RuntimeWarning(f"Failed to auto-install '{name}' because hash_value is missing.")
            elif not target_version and hash_value:
                raise RuntimeWarning(f"Failed to auto-install '{name}' because version is missing.")
            elif not target_version and not hash_value:
                # let's try to make an educated guess and give a useful suggestion
                msg = f"https://pypi.org/pypi/{name}/json"
                response = requests.get(msg)
                if response.status_code == 404:
                    # possibly typo - PEBKAC
                    raise RuntimeWarning(f"Are you sure package '{name}' exists?")
                elif response.status_code != 200:
                    # possibly server problems
                    return fail_or_default(default, Use.AutoInstallationError, f"Tried to look up '{name}' but got a {response.status_code} from PyPI.")
                else: # PEBKAC
                    try:
                        data = response.json()
                        version = data["info"]["version"]
                        hash_value = data["releases"][version][0]["digests"][hash_algo.name]
                    except KeyError:  # json issues
                        raise RuntimeWarning("Please specify version and hash for auto-installation. Sadly something went wrong with the JSON PyPI provided, otherwise we could've provided a suggestion.")
                    raise RuntimeWarning(f"""Please specify version and hash for auto-installation of '{name}'. To use the latest version: 
use("{name}", version="{version}", hash_value="{hash_value}", auto_install=True)
""")

            response = requests.get(f"https://pypi.org/pypi/{name}/{target_version}/json")
            if response.status_code != 200:
                return fail_or_default(default, ImportError, f"Tried to auto-install '{name}' {target_version} but failed with {response} while trying to pull info from PyPI.")
            try:
                if not response.json()["urls"]:
                    return fail_or_default(default, Use.AutoInstallationError, f"Tried to auto-install {name} {target_version} but failed because no valid URLs to download could be found.")
                for entry in response.json()["urls"]:
                    url = entry["url"]
                    that_hash = entry["digests"].get(hash_algo.name)
                    filename = entry["filename"]
                    if entry["yanked"]:
                        return fail_or_default(default, Use.AutoInstallationError, f"Auto-installation of  '{name}' {target_version} failed because the release was yanked from PyPI.")
                    if that_hash == hash_value:
                        break
                else:
                    return fail_or_default(default, Use.AutoInstallationError, f"Tried to auto-install {name} {target_version} but failed because none of the available hashes match the expected hash.")
            except KeyError: # json issues
                exc = traceback.format_exc()
            if exc:
                return fail_or_default(default, Use.AutoInstallationError, f"Tried to auto-install {name} {target_version} but failed because there was a problem with the JSON from PyPI.")
            # we've got a complete JSON with a matching entry, let's download

            # TODO download..
            
            # TODO install..
            
            # TODO load package..
        
        assert mod, "Something went horribly wrong."
        self.set_mod(name=name, mod=mod, path=None, spec=spec, frame=inspect.getframeinfo(inspect.currentframe()))
        return mod

sys.modules["use"] = Use()
