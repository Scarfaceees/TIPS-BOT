"""Phase 3.5.2 / F2 – build image s presnym commitem (TIPSBOT_CODE_REV).

    python scripts/build_release.py              # overi cisty strom, postavi image a overi label i promennou
    python scripts/build_release.py --dry-run    # jen kontrola stromu a vypis prikazu (nic nestavi)

Pravidla:
- build odmitne NECISTY strom: jakakoli zmena nebo nesledovany soubor v cestach, ktere jdou do image
  (src, pyproject.toml, README.md) nebo ho ridi (Dockerfile, docker-compose.yml, .dockerignore);
- commit = `git rev-parse HEAD` (40 znaku) v okamziku buildu; za behu se verze z Gitu NEODHADUJE (v image .git neni);
- po buildu se overi label `org.opencontainers.image.revision` i promenna TIPSBOT_CODE_REV v obrazu.
Image (tipsbot-local) maji obe sluzby spolecny, takze obe dostanou stejny commit. Kontejnery se tu NErestartuji.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_PATHS = ("src", "pyproject.toml", "README.md", "Dockerfile", "docker-compose.yml", ".dockerignore")
IMAGE = "tipsbot-local:latest"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def dirty_paths(repo: Path = ROOT) -> list[str]:
    """Zmeny a nesledovane soubory v cestach buildu (ignorovane soubory podle .gitignore se nepocitaji)."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", *BUILD_PATHS)
    return [line[3:] for line in out.splitlines() if line.strip()]


def head_rev(repo: Path = ROOT) -> str:
    rev = _git(repo, "rev-parse", "HEAD").strip()
    if len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise RuntimeError(f"neplatný commit {rev!r}")
    return rev


def build_command() -> list[str]:
    return ["docker", "compose", "build"]


def verify_image(rev: str, image: str = IMAGE) -> list[str]:
    """Rozdily mezi commitem a obrazem (prazdne = shoda)."""
    label = subprocess.run(["docker", "image", "inspect", image, "-f",
                            '{{ index .Config.Labels "org.opencontainers.image.revision" }}'],
                           check=True, capture_output=True, text=True).stdout.strip()
    env = subprocess.run(["docker", "image", "inspect", image, "-f", "{{ range .Config.Env }}{{ println . }}{{ end }}"],
                         check=True, capture_output=True, text=True).stdout.splitlines()
    env_rev = next((e.split("=", 1)[1] for e in env if e.startswith("TIPSBOT_CODE_REV=")), None)
    bad = []
    if label != rev:
        bad.append(f"label obrazu {label!r} != commit {rev}")
    if env_rev != rev:
        bad.append(f"TIPSBOT_CODE_REV v obrazu {env_rev!r} != commit {rev}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="jen kontrola stromu a výpis příkazu")
    args = ap.parse_args(argv)
    dirty = dirty_paths()
    if dirty:
        print("ODMÍTNUTO: pracovní strom není čistý v cestách buildu:", *(f"  {p}" for p in dirty), sep="\n", file=sys.stderr)
        return 2
    rev = head_rev()
    cmd = build_command()
    print(f"commit {rev}\nTIPSBOT_CODE_REV={rev} {' '.join(cmd)}")
    if args.dry_run:
        return 0
    subprocess.run(cmd, check=True, cwd=ROOT, env={**os.environ, "TIPSBOT_CODE_REV": rev})
    if dirty_paths() or head_rev() != rev:  # strom ani HEAD se behem buildu nesmely zmenit
        print("CHYBA: během buildu se změnil pracovní strom nebo HEAD – image nepoužívej", file=sys.stderr)
        return 3
    bad = verify_image(rev)
    if bad:
        print("CHYBA:", *bad, sep="\n  ", file=sys.stderr)
        return 4
    print(f"OK: {IMAGE} = commit {rev} (label i TIPSBOT_CODE_REV)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
