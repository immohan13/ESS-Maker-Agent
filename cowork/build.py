"""Build the ESS Doctor Copilot Cowork plugin.

    python cowork/build.py            # from the ess-maker folder

Produces dist/ess-doctor/ (Claude-style plugin layout that Cowork converts on upload) and
dist/ess-doctor-cowork-plugin.zip. Each skill carries the same scripts/essdoctor.pyz: the standard-library
parts of the ESS Doctor engine (src/ess_maker/doctor) plus the CLI in cowork/essdoctor_cli.py.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipapp
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src" / "ess_maker"
DIST = ROOT / "dist"
PLUGIN_SRC = HERE / "plugin"

# Engine modules that only use the standard library. The web app, live Copilot Studio access, Kusto and
# Azure OpenAI modules stay out: the plugin works on files the maker uploads.
ENGINE = ["agent_file", "blocks", "condense", "devmode", "devmode_report", "devui", "drafter", "evaltriage", "evidence",
          "fixes", "known_issues", "practices", "report", "rules", "setup_servicenow", "sorry"]

SETTINGS_STUB = '''"""Settings stub for the Cowork plugin: no Azure OpenAI, so the drafter uses its deterministic wording."""


class _Settings:
    azure_openai_endpoint = ""
    azure_openai_api_key = ""

    def __getattr__(self, name):
        return ""


settings = _Settings()
'''


def build_pyz(out: Path) -> None:
    stage = DIST / "pyz-stage"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    if stage.exists():
        stage = DIST / f"pyz-stage-{__import__('os').getpid()}"
    pkg = stage / "ess_maker"
    (pkg / "doctor").mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""ESS Doctor engine (Cowork plugin build)."""\n', encoding="utf-8")
    (pkg / "settings.py").write_text(SETTINGS_STUB, encoding="utf-8")
    shutil.copy(SRC / "doctor" / "__init__.py", pkg / "doctor" / "__init__.py")
    for m in ENGINE:
        src = SRC / "doctor" / f"{m}.py"
        if not src.exists():
            sys.exit(f"Missing engine module: {src}")
        shutil.copy(src, pkg / "doctor" / f"{m}.py")
    lib = SRC / "doctor" / "condense_library.json"
    if lib.exists():
        shutil.copy(lib, pkg / "doctor" / lib.name)
    tpl = stage / "templates"
    tpl.mkdir()
    (tpl / "__init__.py").write_text("", encoding="utf-8")
    for p in ("hr", "it", "hub"):
        shutil.copy(HERE / "templates" / f"template_{p}.txt", tpl / f"template_{p}.txt")
    shutil.copy(HERE / "essdoctor_cli.py", stage / "__main__.py")
    out.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3")
    shutil.rmtree(stage, ignore_errors=True)


LOADER = '''#!/usr/bin/env python3
"""ESS Doctor engine. Run: python3 essdoctor.py --help

The engine modules are embedded below as a zip archive (Cowork skills accept .py companion files but not .pyz,
and allow at most 20 files per skill). The archive is unpacked to a temporary folder and run from there.
"""
import base64, hashlib, os, runpy, sys, tempfile

_ENGINE = "{payload}"

def _main():
    data = base64.b64decode(_ENGINE)
    path = os.path.join(tempfile.gettempdir(), "essdoctor-" + hashlib.sha256(data).hexdigest()[:12] + ".pyz")
    if not os.path.exists(path):
        tmp = path + ".%d" % os.getpid()
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    sys.path.insert(0, path)
    sys.argv[0] = "essdoctor"
    runpy.run_path(path, run_name="__main__")

if __name__ == "__main__":
    _main()
'''


def build_loader(pyz: Path, out: Path) -> None:
    import base64

    out.write_text(LOADER.replace("{payload}", base64.b64encode(pyz.read_bytes()).decode("ascii")), encoding="utf-8", newline="\n")


def main() -> None:
    plugin = DIST / "ess-doctor"
    if plugin.exists():
        # Remove files only; some synced folders (OneDrive) refuse to delete directories that were just used.
        for f in sorted(plugin.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink()
    shutil.copytree(PLUGIN_SRC, plugin, dirs_exist_ok=True)
    pyz = DIST / "essdoctor.pyz"
    build_pyz(pyz)
    script = DIST / "essdoctor.py"
    build_loader(pyz, script)
    skills = sorted(p for p in (plugin / "skills").iterdir() if (p / "SKILL.md").exists())
    for s in skills:
        md = s / "SKILL.md"
        md.write_bytes(md.read_bytes().replace(b"\r\n", b"\n").replace(b"scripts/essdoctor.pyz", b"scripts/essdoctor.py"))  # frontmatter parsers on Linux expect LF
        (s / "scripts").mkdir(exist_ok=True)
        shutil.copy(script, s / "scripts" / "essdoctor.py")
        files = [f for f in s.rglob("*") if f.is_file() and f.name != "SKILL.md"]
        size = sum(f.stat().st_size for f in files)
        if len(files) > 20 or size > 10_000_000 or any(f.stat().st_size > 5_000_000 for f in files):
            sys.exit(f"{s.name}: companion files exceed Cowork limits (20 files, 5 MB each, 10 MB total).")
    manifest = json.loads((plugin / "manifest.json").read_text(encoding="utf-8"))
    folders = {Path(e["folder"]).name for e in manifest.get("agentSkills", [])}
    if folders != {s.name for s in skills}:
        sys.exit(f"manifest.json agentSkills {sorted(folders)} don't match the skill folders {sorted(s.name for s in skills)}.")
    for s in skills:
        fm = (s / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        name = next((ln.split(":", 1)[1].strip() for ln in fm.splitlines() if ln.startswith("name:")), "")
        if name != s.name or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
            sys.exit(f"{s.name}: skill name must be kebab-case and match the folder (ASKILL-P006/P007).")
    zpath = DIST / "ess-doctor-cowork-plugin.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(plugin.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(plugin).as_posix())
    print(f"{manifest['name']['short']} {manifest['version']}: {len(skills)} skills, pyz {pyz.stat().st_size:,} bytes")
    print(f"Plugin folder: {plugin}")
    print(f"Upload this to Cowork: {zpath} ({zpath.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
