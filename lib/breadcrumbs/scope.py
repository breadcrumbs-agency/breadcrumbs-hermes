"""scope: the project of a place on disk owns a context.

The project of a path is the nearest of:

  1. a folder that holds a `.breadcrumbs` entry (a file or a folder): the explicit project, at any depth. A file whose
     text is `off` (or `none`, `default`) says the opposite: this place has no project, use the default memory;
  2. a git repository (a linked worktree or a submodule counts as its main repository);
  3. a source tree: a folder with a project file (pyproject.toml, package.json, Cargo.toml, ...);
  4. else the folder itself (the current directory) is the project.

So a project exists wherever a chat works, "when no metadata says otherwise": the git repository, or the current
directory. The home folder is the one exception: its project is the profile's default memory (`workdir.py`).

Projects nest: a monorepo that holds repositories, a `.breadcrumbs` folder inside a repository. chain(path) lists every
explicit project and git repository that holds a path, the nearest first. Automatic recall and every write use the
nearest project only, so one codebase never leaks into another. A read goes up the chain only when it is asked to
(scope "up"), and across all projects with "all".

The memory of a project is a store folder under <base>/scopes/<id>, where <base> is the breadcrumbs folder of the home
(<home>/breadcrumbs) and <id> is the folder name plus eight hex digits of the SHA-256 of the real path. The store is
not in the repository, so no memory reaches git. Nothing here creates, switches or clones a Hermes profile.
"""
import hashlib
import json
import os
import re

MARKER = ".breadcrumbs"
SOURCE_MARKERS = ("pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
                  "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "CMakeLists.txt", "Makefile", "Gemfile",
                  "composer.json", "mix.exs", "Package.swift")
EXPLICIT, GIT, SOURCE, DIR = "explicit", "git", "source", "dir"
OFF_WORDS = ("off", "none", "default", "no")
META = "scope.json"
HOME = os.path.expanduser("~")


def _git_root(d):
    """The repository that folder d holds, or None. A .git file (a linked worktree, a submodule) names a folder in
    the main repository's .git: the scope is that main repository."""
    git = os.path.join(d, ".git")
    if os.path.isdir(git):
        return d
    try:
        with open(git, encoding="utf-8") as f:
            m = re.match(r"gitdir:\s*(.+?)\s*$", f.readline())
    except (OSError, UnicodeDecodeError):
        return None
    if not m:
        return None
    main, sep, _ = os.path.normpath(os.path.join(d, m.group(1))).partition(os.sep + ".git" + os.sep)
    return main if sep else d


def _marker_off(d):
    """True when the `.breadcrumbs` file of folder d says off: no project here, the default memory."""
    path = os.path.join(d, MARKER)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(256).strip().lower()
    except (OSError, UnicodeDecodeError):
        return False
    return text in OFF_WORDS or text.replace(" ", "") in ('{"scope":"off"}', '{"project":"off"}')


def _explicit(d):
    return d if os.path.exists(os.path.join(d, MARKER)) else None


def _source(d):
    return d if any(os.path.exists(os.path.join(d, m)) for m in SOURCE_MARKERS) else None


def _start(path):
    """The folder to climb from: the path itself when it is a folder, else its nearest existing parent."""
    p = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    while p != os.path.dirname(p) and not os.path.isdir(p):
        p = os.path.dirname(p)
    return p


def _excluded(root, exclude):
    return any(root == x or root.startswith(x + os.sep) for x in exclude)


def _roots(path, home, exclude):
    """[(kind, root)] for every folder from `path` up that is a project root, the nearest first: explicit markers and
    git repositories. With none, the nearest source tree; with none, the folder itself (kind "dir"). [] when the nearest
    marker says off, or when the folder is in `exclude`. (`home` is only for callers that pass it; it limits nothing.)"""
    exclude = [os.path.abspath(x) for x in exclude or ()]
    start = p = _start(path)
    if _excluded(start, exclude):
        return []
    out, seen = [], set()
    while True:
        for kind, found in ((EXPLICIT, _explicit), (GIT, _git_root)):
            r = found(p)
            if r and r not in seen and not _excluded(r, exclude):
                if kind == EXPLICIT and _marker_off(r):
                    return out  # nothing nearer than the marker: no project; a nearer repository was found first
                seen.add(r)
                out.append((kind, r))
        if p == os.path.dirname(p):
            break
        p = os.path.dirname(p)
    if out:
        return out
    p = start  # no marker and no repository: the nearest source tree, else the folder itself
    while True:
        r = _source(p)
        if r and not _excluded(r, exclude):
            return [(SOURCE, r)]
        if p == os.path.dirname(p):
            break
        p = os.path.dirname(p)
    return [(DIR, start)]


def chain(path=None, home=None, exclude=()):
    """[(kind, root)] of every scope that holds `path`, the nearest first. [] when the path has no scope."""
    return _roots(path, home, exclude)


def nearest(path=None, home=None, exclude=()):
    """(kind, root) of the scope of `path`, or None."""
    got = _roots(path, home, exclude)
    return got[0] if got else None


def codebase_of(path, home=None, exclude=()):
    """The root folder of the scope of `path`, or None (the one-word form of nearest)."""
    got = nearest(path, home, exclude)
    return got[1] if got else None


def scope_id(root):
    real = os.path.realpath(root)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(real.rstrip(os.sep)) or "root").strip("-.")[:40] or "root"
    return "%s-%s" % (name, hashlib.sha256(real.encode("utf-8")).hexdigest()[:8])


def store_home(base, root):
    """The folder of the scope's store: <base>/scopes/<id>."""
    return os.path.join(base, "scopes", scope_id(root))


def ensure(base, kind, root):
    """The store folder of a scope, made, with scope.json (its root and kind) so the scopes can be listed."""
    home = store_home(base, root)
    os.makedirs(home, exist_ok=True)
    meta = {"id": scope_id(root), "root": os.path.realpath(root), "kind": kind}
    path = os.path.join(home, META)
    try:
        with open(path, encoding="utf-8") as f:
            if json.load(f) == meta:
                return home
    except (OSError, ValueError):
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, path)
    return home


def known(base):
    """Every scope that has a store under <base>/scopes: [{"id", "root", "kind", "path", "present"}], by id."""
    out = []
    folder = os.path.join(base, "scopes")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        path = os.path.join(folder, name)
        try:
            with open(os.path.join(path, META), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({"id": meta.get("id", name), "root": meta.get("root", ""), "kind": meta.get("kind", ""),
                    "path": path, "present": os.path.isfile(os.path.join(path, "index.db"))})
    return out


def short(path, home=None):
    """A path for a message: ~/x under the home folder."""
    home = os.path.abspath(home or HOME)
    return "~" + path[len(home):].replace(os.sep, "/") if path.startswith(home + os.sep) else path
