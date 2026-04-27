"""Power Platform CLI (`pac`) integration for ESS Maker.

Wraps the bits we actually need:
  • detect that pac is installed and signed in
  • pack an unpacked solution folder into a .zip
  • import + publish that .zip into the configured environment

Everything is subprocess-based; no auth state is stored in this process.
The maker does `pac auth create` once out of band — we just consume whatever
auth profile is currently active.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PAC_BIN = "pac"  # resolved via PATH; pac.cmd on Windows works fine


def _which_pac() -> str | None:
    return shutil.which(PAC_BIN) or shutil.which("pac.cmd")


@dataclass
class PacRunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


async def _run(args: list[str], cwd: Path | None = None, timeout: float = 600.0) -> PacRunResult:
    """Run `pac <args>` and capture full stdout/stderr."""
    bin_path = _which_pac()
    if not bin_path:
        return PacRunResult(False, "", "pac CLI not found on PATH", 127)
    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return PacRunResult(False, "", f"pac {' '.join(args)} timed out after {timeout}s", -1)
    return PacRunResult(
        ok=proc.returncode == 0,
        stdout=(out or b"").decode(errors="replace"),
        stderr=(err or b"").decode(errors="replace"),
        returncode=proc.returncode or 0,
    )


# ---------- Status / capability ---------------------------------------------

async def pac_status() -> dict[str, Any]:
    """Report whether pac is installed, what version, and what auth profile is active."""
    bin_path = _which_pac()
    if not bin_path:
        return {
            "installed": False,
            "path": None,
            "version": None,
            "auth": None,
            "hint": "Install Power Platform CLI: https://aka.ms/PowerAppsCLI",
        }
    # `pac help` prints a header containing "Microsoft PowerPlatform CLI Version: X.Y.Z..."
    ver_run = await _run(["help"], timeout=15)
    version = None
    for line in (ver_run.stdout or "").splitlines():
        if "Version" in line and "PowerPlatform" in line:
            version = line.strip()
            break

    auth = await _run(["auth", "list"], timeout=20)
    active = _parse_active_auth(auth.stdout) if auth.ok else None
    return {
        "installed": True,
        "path": bin_path,
        "version": version,
        "auth": active,
        "auth_raw": auth.stdout if not active else None,
        "hint": (
            "Run `pac auth create --environment <ENV_GUID>` to sign in."
            if not active else None
        ),
    }


def _parse_active_auth(text: str) -> dict[str, str] | None:
    """`pac auth list` columns: Index | Active | Kind | Name | User | Cloud | Type | Env | Env URL.

    The active row has '*' in the Active column. We split on whitespace and
    treat any line whose 2nd whitespace-delimited token is exactly '*' as the
    active profile.
    """
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("index"):
            continue
        toks = line.split()
        if len(toks) >= 2 and toks[1] == "*":
            return {
                "active": True,
                "raw": line,
                "user": _pick(toks, 4),       # may be empty in some layouts
                "environment": _pick(toks, -2),
                "environment_url": _pick(toks, -1),
            }
    return None


def _pick(toks: list[str], idx: int) -> str:
    try:
        return toks[idx]
    except IndexError:
        return ""


# ---------- Pack + import ----------------------------------------------------

# Default repo layout. The HR solution lives under sources/dev/solutions/CopilotForEmployeeSelfServiceHR/Solution.
def default_solution_dir(repo_root: Path) -> Path:
    return (
        repo_root
        / "sources" / "dev" / "solutions"
        / "CopilotForEmployeeSelfServiceHR" / "Solution"
    )


async def pack_and_import(
    solution_src_dir: Path,
    out_zip: Path,
    *,
    environment_id: str | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Pack the unpacked solution → import into the active environment → publish.

    Returns a structured log of every step. Stops on the first failure.
    """
    log: list[dict[str, Any]] = []

    if not solution_src_dir.exists():
        return {
            "ok": False,
            "step": "validate",
            "error": f"solution source folder does not exist: {solution_src_dir}",
            "log": log,
        }

    # Pre-flight: refuse to try to import a Managed / first-party solution.
    # Power Platform requires a signed CAB for those and pac will reject the
    # zip with a confusing "Cab file required" error.
    pre = _inspect_solution(solution_src_dir)
    log.append({"step": "inspect solution", "ok": True, "info": pre})
    if pre.get("blocked"):
        return {
            "ok": False,
            "step": "inspect solution",
            "error": pre["reason"],
            "hint": pre.get("hint"),
            "solution": pre,
            "log": log,
        }

    # Step 1: optional pin to env if caller provided one.
    if environment_id:
        sel = await _run(["org", "select", "--environment", environment_id], timeout=60)
        log.append({"step": "org select", "ok": sel.ok, "stdout": sel.stdout[-2000:], "stderr": sel.stderr[-2000:]})
        if not sel.ok:
            return {"ok": False, "step": "org select", "log": log,
                    "error": "could not select target environment — check `pac auth list`"}

    # Step 2: pack
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # `pac solution init` writes the unpacked tree under <folder>/src/.
    # `pac solution pack --folder` must point at the dir that contains Other/Solution.xml.
    pack_folder = solution_src_dir
    if (solution_src_dir / "src" / "Other" / "Solution.xml").exists():
        pack_folder = solution_src_dir / "src"
    pack = await _run([
        "solution", "pack",
        "--zipfile", str(out_zip),
        "--folder", str(pack_folder),
        "--packagetype", "Unmanaged",
    ], timeout=600)
    log.append({"step": "solution pack", "ok": pack.ok, "stdout": pack.stdout[-2000:], "stderr": pack.stderr[-2000:]})
    if not pack.ok:
        return {"ok": False, "step": "solution pack", "log": log,
                "error": "pack failed — check that the source folder is a valid unpacked solution"}

    # Step 3: import
    import_args = [
        "solution", "import",
        "--path", str(out_zip),
        "--activate-plugins",
        "--force-overwrite",
    ]
    if publish:
        import_args.append("--publish-changes")
    imp = await _run(import_args, timeout=1800)
    log.append({"step": "solution import", "ok": imp.ok, "stdout": imp.stdout[-4000:], "stderr": imp.stderr[-4000:]})
    if not imp.ok:
        return {"ok": False, "step": "solution import", "log": log,
                "error": "import failed — see stderr"}

    return {
        "ok": True,
        "log": log,
        "zip_path": str(out_zip),
        "next_steps": [
            "Open Copilot Studio and confirm the new topic appears under the HR agent.",
            "Re-run the smoke eval from the **Run Eval** tab to verify the topic responds.",
        ],
    }


# ---------- Solution pre-flight --------------------------------------------

import re as _re


def _inspect_solution(folder: Path) -> dict[str, Any]:
    """Read Solution.xml to detect publisher / managed status.

    Returns {unique_name, version, publisher, managed, blocked, reason, hint}.
    Sets blocked=True for managed or first-party solutions which cannot be
    re-imported as unmanaged via `pac solution import`.

    `<Managed>` values in Solution.xml:
      0 = unmanaged source, 1 = managed source (blocked),
      2 = dual / either-format source (default for `pac solution init`, NOT blocked).
    """
    # `pac solution init` writes <folder>/src/Other/Solution.xml; legacy
    # unpacks put it at <folder>/Other/Solution.xml.
    candidates = [
        folder / "Other" / "Solution.xml",
        folder / "src" / "Other" / "Solution.xml",
    ]
    sxml = next((p for p in candidates if p.exists()), None)
    if sxml is None:
        try:
            sxml = next(folder.rglob("Solution.xml"))
        except StopIteration:
            return {"blocked": False, "reason": "no Solution.xml found (proceeding anyway)"}

    text = sxml.read_text(encoding="utf-8", errors="replace")
    def _find(pattern: str) -> str:
        m = _re.search(pattern, text)
        return (m.group(1).strip() if m else "")

    unique = _find(r"<UniqueName>\s*([^<]+?)\s*</UniqueName>")
    publisher = _find(r"<Publisher>.*?<UniqueName>\s*([^<]+?)\s*</UniqueName>")
    if not publisher:
        publisher = _find(r"PublisherUniqueName=['\"]([^'\"]+)")
    version = _find(r"<Version>\s*([^<]+?)\s*</Version>")
    managed_raw = _find(r"<Managed>\s*([^<]+?)\s*</Managed>")
    # Only `1` (or true) means a managed-source solution that can't be re-imported.
    # `2` is the default `pac solution init` value ("either format") and is safe.
    managed = managed_raw.strip() in ("1", "true", "True")

    info: dict[str, Any] = {
        "path": str(sxml),
        "unique_name": unique,
        "publisher": publisher,
        "version": version,
        "managed_raw": managed_raw,
        "managed": managed,
        "blocked": False,
    }
    if managed:
        info["blocked"] = True
        info["reason"] = (
            f"Solution `{unique or folder.name}` is **managed** "
            f"(publisher `{publisher or 'unknown'}`). Managed solutions can't be repacked + "
            "re-imported with `pac solution import` — Power Platform requires a signed CAB."
        )
        info["hint"] = (
            "Create your own *unmanaged* customisation solution in the target environment "
            "(Power Apps → Solutions → New solution, with your own publisher), add the HR "
            "agent's components to it, then point ESS Maker at that folder via "
            "**target_solution_subdir**."
        )
    elif publisher.lower().startswith("microsoftfirstparty"):
        info["blocked"] = True
        info["reason"] = (
            f"Solution `{unique}` belongs to publisher `microsoftfirstparty` and cannot be "
            "modified/re-imported by makers. You need an unmanaged customisation solution."
        )
        info["hint"] = info.get("hint") or (
            "Create your own unmanaged solution in Power Apps and point ESS Maker at it via "
            "**target_solution_subdir**."
        )
    return info


# ---------- Solution init helper -------------------------------------------

async def init_unmanaged_solution(
    out_dir: Path,
    solution_name: str,
    publisher_prefix: str,
) -> dict[str, Any]:
    """Run `pac solution init` to scaffold a new unmanaged solution folder.

    Returns {ok, path, log, error?, hint?}.
    """
    if not solution_name.strip() or not publisher_prefix.strip():
        return {"ok": False, "error": "solution_name and publisher_prefix are required"}
    if not _re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{1,7}", publisher_prefix):
        return {
            "ok": False,
            "error": f"publisher prefix `{publisher_prefix}` is invalid",
            "hint": "Use 2-8 letters/digits starting with a letter, e.g. `essmkr`.",
        }
    out_dir.mkdir(parents=True, exist_ok=True)
    res = await _run([
        "solution", "init",
        "--publisher-name", solution_name,
        "--publisher-prefix", publisher_prefix,
        "--outputDirectory", str(out_dir),
    ], timeout=60)
    if not res.ok:
        return {
            "ok": False,
            "error": "pac solution init failed",
            "stdout": res.stdout[-2000:],
            "stderr": res.stderr[-2000:],
            "hint": "Make sure pac is installed and you have write access to the target folder.",
        }
    return {"ok": True, "path": str(out_dir), "stdout": res.stdout[-1000:]}
