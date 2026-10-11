"""Ключи в коммитах — gitleaks (AGENTS.md, «Ключи и деньги»; почему он — docs/decisions.md).

    python3 tools/secret_scan.py staged              хук pre-commit: индекс; нет gitleaks — ставит
    python3 tools/secret_scan.py range <база>..<голова>   CI: все коммиты PR
    python3 tools/secret_scan.py install [--binary]  поставить закреплённую версию
    python3 tools/secret_scan.py version             какая стоит
    python3 tools/secret_scan.py --selftest          подложенный ключ ловится, чистый коммит — нет

Сторож не даёт прочитать `.env`, но ключ, вписанный в код или конфиг, ушёл бы в коммит —
его останавливает этот хук, а обход `--no-verify` ловит CI. Исключения — `.gitleaks.toml`
в корне, у каждого — причина.

Ставится одна версия, VERSION. Есть Go — `go install` (целостность — sumdb Go; так и в
облачной сессии, где загрузки с github.com закрыты); иначе или с `--binary` — архив релиза
с github.com, сверенный по файлу контрольных сумм того же релиза.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from script_common import ROOT, git_env, report

VERSION = "8.30.1"
MODULE = "github.com/zricethezav/gitleaks/v8"
RELEASES = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}"
CONFIG = ".gitleaks.toml"
#: Куда класть бинарь из архива; go install кладёт в свой GOBIN.
LOCAL_BIN = Path.home() / ".local" / "bin"
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def go_bin() -> Path:
    gobin = subprocess.run(["go", "env", "GOBIN", "GOPATH"], capture_output=True,  # noqa: S607
                           text=True, check=True).stdout.split("\n")
    return Path(gobin[0]) if gobin[0] else Path(gobin[1]) / "bin"


def find() -> Path | None:
    """Путь к gitleaks: PATH, затем места, куда его кладёт install."""
    if found := shutil.which("gitleaks"):
        return Path(found)
    places = [LOCAL_BIN / "gitleaks"] + ([go_bin() / "gitleaks"] if shutil.which("go") else [])
    return next((p for p in places if p.exists()), None)


def install_go() -> None:
    flag = f"-X {MODULE}/version.Version=v{VERSION}"
    subprocess.run(["go", "install", "-ldflags", flag, f"{MODULE}@v{VERSION}"],  # noqa: S603, S607
                   check=True)


def download(name: str) -> bytes:
    with urllib.request.urlopen(f"{RELEASES}/{name}", timeout=120) as answer:  # noqa: S310
        return answer.read()


def install_binary() -> None:
    system = platform.system().lower()
    name = f"gitleaks_{VERSION}_{system}_{ARCH[platform.machine().lower()]}.tar.gz"
    archive = download(name)
    sums = dict(line.split()[::-1] for line in
                download(f"gitleaks_{VERSION}_checksums.txt").decode().splitlines() if line)
    if hashlib.sha256(archive).hexdigest() != sums.get(name):
        sys.exit(f"контрольная сумма {name} не сходится — не ставлю")
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.extractfile("gitleaks")
        (LOCAL_BIN / "gitleaks").write_bytes(member.read())
    (LOCAL_BIN / "gitleaks").chmod(0o755)


def install(binary: bool) -> Path:
    (install_binary if binary or not shutil.which("go") else install_go)()
    found = find()
    if found is None:
        sys.exit("gitleaks поставлен, но не найден: добавить ~/.local/bin или GOBIN в PATH")
    return found


def scan(root: Path, *args: str) -> int:
    """gitleaks git с исключениями репозитория; код 1 — найден ключ."""
    found = find() or install(binary=False)
    config = ["--config", str(root / CONFIG)] if (root / CONFIG).exists() else []
    done = subprocess.run([str(found), "git", *args, "--redact", "--no-banner", "--verbose",  # noqa: S603
                           *config, str(root)], cwd=root, env=git_env(), check=False)
    if done.returncode:
        print("коммит с ключом остановлен: ключ убрать в .env и сменить — он мог уйти дальше "
              f"(AGENTS.md, «Ключи и деньги»); не ключ — исключение в {CONFIG}", file=sys.stderr)
    return 1 if done.returncode else 0


# ---------------------------------------------------------------------------
# selftest: временный репозиторий, ключ в индексе и в истории
# ---------------------------------------------------------------------------

#: Подложенный ключ → правило gitleaks, которое обязано его поймать.
PLANTED = {"aws-access-token": ("AKIA", "QZXKWMRPTBLNHJ27"),
           "openai-api-key": ("sk-proj-", "Zq8Rk2Lp7Wx4Vn9Bt3Hc6Jy1Mf5Gd0Sa" * 2 + "Xy7Kq9Pw3Z"
                              + "T3Blbk" + "FJ" + "Mn4Bv8Cx2Lk6Jh0Gf9Ds3Ap7Qw5Er1Ty" * 2
                              + "Ui8Op3Za5X"),
           "anthropic-api-key": ("sk-ant-", "api03-" + "Wp4Rt8Yu2Io6Pa0Sd3Fg7Hj1Kl5Zx9Cv" * 2
                                 + "Wp4Rt8Yu2Io6Pa0Sd3Fg7Hj1Kl5Zx" + "AA")}


def planted() -> str:
    """Ключи собираются при запуске: в самом файле их нет, иначе gitleaks поймал бы и его."""
    return "".join(f'{rule.replace("-", "_")} = "{head}{tail}"\n'
                   for rule, (head, tail) in PLANTED.items())


def rules_found(root: Path, *args: str) -> set[str]:
    """Какие правила сработали — по отчёту gitleaks в JSON."""
    found = find()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        subprocess.run([str(found), "git", *args, "--no-banner", "--log-level", "error",  # noqa: S603
                        "--report-format", "json", "--report-path", str(out), str(root)],
                       cwd=root, env=git_env(), capture_output=True, check=False)
        return {f["RuleID"] for f in json.loads(out.read_text() or "[]")}


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S603, S607
                   cwd=root, capture_output=True, check=True, env=git_env())


def selftest() -> int:
    if find() is None:
        return report(["gitleaks не стоит: python3 tools/secret_scan.py install"], "")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git(root, "init", "-q")
        (root / "app.py").write_text('KEY = os.environ["KEY"]\n')
        git(root, "add", "app.py")
        clean = rules_found(root, "--staged")
        git(root, "commit", "-qm", "feat: начало")
        (root / "conf.py").write_text(planted())
        git(root, "add", "conf.py")
        staged = rules_found(root, "--staged")
        git(root, "commit", "-qm", "feat: ключ")
        history = rules_found(root, "--log-opts=HEAD~1..HEAD")
    bad = [f"чистый индекс остановлен: {clean}"] if clean else []
    bad += [f"{where}: не пойман {rule}" for rule in PLANTED
            for where, got in [("индекс", staged), ("коммит", history)] if rule not in got]
    return report(bad, f"gitleaks {VERSION}: {', '.join(PLANTED)} пойманы в индексе и в "
                       "коммите; чистое не останавливается")


def show_version() -> int:
    found = find()
    print(subprocess.run([str(found), "version"], capture_output=True, text=True,  # noqa: S603
                         check=False).stdout.strip() if found else "не стоит")
    return 0 if found else 1


def cmd_install(binary: bool) -> int:
    print(f"gitleaks {VERSION}: {install(binary)}")
    return 0


def main() -> int:
    args = sys.argv[1:] + [""]
    commands = {
        "--selftest": selftest,
        "staged": lambda: scan(ROOT, "--staged"),
        "range": lambda: scan(ROOT, f"--log-opts={args[1]}") if args[1] else 2,
        "install": lambda: cmd_install("--binary" in args),
        "version": show_version,
    }
    if args[0] in commands:
        return commands[args[0]]()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
