"""Проверка перед передачей наружу: ключи, внутреннее, исходники (AGENTS.md, «Ключи и деньги»;
порядок — docs/agents/protection.md, «Передача наружу»).

    python3 tools/handover_check.py <файл или папка> …   код 0 — можно передавать, 1 — нет
    python3 tools/handover_check.py --text "<текст сообщения>"
    python3 tools/handover_check.py --selftest           каждый вид находки ловится, чистое — нет

Наружу — продакту, заказчику, третьим лицам — уходят только результаты — бланк, документ,
сообщение. Красный — не передаём. Значения секретов не печатаются: только вид находки, имя
правила gitleaks или имя переменной `.env`. Картинки и нечитаемое — не находка, но
печатаются списком «смотреть глазами»: их читает человек.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from script_common import ROOT, report
from secret_scan import CONFIG, find

SOURCE_SUFFIXES = {".py", ".pyc", ".pyw", ".sh", ".ps1", ".bat", ".cmd", ".bsl", ".os", ".js",
                   ".ts", ".go", ".rs", ".java", ".cs", ".ipynb"}
SERVICE_PARTS = {".git", ".claude", ".agents", ".codex", ".cursor", ".github"}
SERVICE_SEQUENCES = ("docs/tasks", "docs/journal")
INTERNAL_NAMES = {"AGENTS.md", "CLAUDE.md", "JOURNAL.md", "INSIGHTS.md"}
ENV_NAME = re.compile(r"^\.env(\..+)?$")
ZIP_SUFFIXES = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".zip"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
MIN_ENV_VALUE = 8
NO_GITLEAKS = "gitleaks не найден — python3 tools/secret_scan.py install"
TAG = re.compile(r"<[^>]+>")

INTERNAL_TEXT = {
    "ссылка на задачу": r"\b(?:{prefix}|wl|tpl)#\d+\b",
    "путь docs/tasks/": r"docs/tasks/",
    "служебная строка задачи": r"Чем возобновлять|Локальный повтор|ниже наше: скрипт",
}
SECRET_TEXT = {
    "закрытый ключ": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "пароль или токен в виде «имя = значение»":
        r"""(?i)\b(password|passwd|пароль|secret|token|api[_-]?key|client_secret)\b"""
        r"""\s*[:=]\s*["']?[^\s"']{6,}""",
    "токен GitHub": r"ghp_[A-Za-z0-9]{30,}|github_pat_\w{20,}",
    "ключ вида sk-": r"sk-[A-Za-z0-9_-]{20,}",
    "токен Telegram-бота": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
}


@dataclass(frozen=True)
class Profile:
    """Кому передаём. По умолчанию — наружу (продакт, заказчик): все находки. Описание продукта
    для инженеров-соавторов (свой профиль у сборки описания) разрешает часть внутреннего
    (`allow` — только имена из INTERNAL_TEXT: ключи и пароли не разрешаются никому) и
    добавляет свои образцы (`extra`: имя → регулярное выражение)."""

    allow: frozenset[str] = frozenset()
    extra: tuple[tuple[str, str], ...] = ()


OUTSIDE = Profile()


def text_patterns(root: Path, profile: Profile = OUTSIDE) -> dict[str, re.Pattern[str]]:
    """Образцы текста; префикс задач — из `.backlog.json` корня."""
    if unknown := profile.allow - set(INTERNAL_TEXT):
        raise ValueError(f"разрешить можно только внутреннее ({', '.join(INTERNAL_TEXT)}), "
                         f"а не {', '.join(sorted(unknown))}")
    try:
        prefix = json.loads((root / ".backlog.json").read_text(encoding="utf-8"))["prefix"]
    except (OSError, ValueError, KeyError):
        prefix = "we"
    sources = {**INTERNAL_TEXT, **SECRET_TEXT, **dict(profile.extra)}
    return {name: re.compile(p.replace("{prefix}", re.escape(prefix)))
            for name, p in sources.items() if name not in profile.allow}


def env_values(root: Path) -> dict[str, str]:
    """Значения `.env` корня длиной от MIN_ENV_VALUE: имя → значение."""
    try:
        lines = (root / ".env").read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    pairs = (line.split("=", 1) for line in lines
             if "=" in line and not line.lstrip().startswith("#"))
    values = {k.strip().removeprefix("export "): v.strip().strip("\"'") for k, v in pairs}
    return {k: v for k, v in values.items() if len(v) >= MIN_ENV_VALUE}


def path_findings(rel: str) -> list[str]:
    """Служебный путь или исходник — по самому пути."""
    parts = Path(rel).parts
    name = parts[-1] if parts else ""
    found = []
    if Path(name).suffix.lower() in SOURCE_SUFFIXES:
        found.append("исходник")
    if SERVICE_PARTS & set(parts) or any(f"/{s}/" in f"/{Path(rel).as_posix()}/"
                                         for s in SERVICE_SEQUENCES):
        found.append("служебный путь")
    if name in INTERNAL_NAMES or ENV_NAME.match(name):
        found.append("внутренний файл")
    return found


def text_findings(text: str, patterns: dict[str, re.Pattern[str]],
                  env: dict[str, str]) -> list[str]:
    found = [name for name, pattern in patterns.items() if pattern.search(text)]
    return found + [f"значение {key} из .env" for key, value in env.items() if value in text]


def read_pdf(path: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    done = subprocess.run(["pdftotext", str(path), "-"], capture_output=True,  # noqa: S603, S607
                          text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def read_zip(path: Path) -> dict[str, str] | None:
    """Член архива → текст (xml — без тегов); нечитаемый архив — None."""
    members = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                raw = archive.read(info).decode("utf-8", errors="ignore")
                is_xml = info.filename.lower().endswith((".xml", ".rels"))
                members[info.filename] = TAG.sub("", raw) if is_xml else raw
    except (zipfile.BadZipFile, OSError):
        return None
    return members


def read_plain(path: Path) -> str | None:
    """Текст обычного файла; бинарный (есть нулевой байт) — None."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in raw[:8192] else raw.decode("utf-8", errors="ignore")


def file_texts(path: Path) -> tuple[dict[str, str], bool]:
    """Тексты файла: ключ — «» или имя члена архива; флаг — читать глазами."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return {}, True
    if suffix in ZIP_SUFFIXES:
        members = read_zip(path)
        return (members or {}), members is None
    if suffix == ".pdf":
        text = read_pdf(path)
        return ({"": text} if text is not None else {}), text is None
    text = read_plain(path)
    return ({"": text} if text is not None else {}), text is None


def check_file(path: Path, shown: str, patterns: dict[str, re.Pattern[str]],
               env: dict[str, str]) -> tuple[list[str], bool]:
    """Находки одного файла и признак «смотреть глазами»."""
    found = [f"✗ {shown}: {kind}" for kind in path_findings(shown)]
    texts, eyes = file_texts(path)
    for member, text in texts.items():
        label = f"{shown}!{member}" if member else shown
        found += [f"✗ {label}: {kind}" for kind in text_findings(text, patterns, env)]
        found += [f"✗ {label}: {kind}" for kind in path_findings(member) if member]
    return found, eyes


def shown_path(file: Path, arg: Path, root: Path) -> str:
    """Путь для показа и проверки: от корня репо, если файл в нём, иначе — от папки над
    аргументом; служебные папки выше (`~/.claude/…`) находкой не считаются."""
    file = file.resolve()
    base = root.resolve() if file.is_relative_to(root.resolve()) else arg.resolve().parent
    return file.relative_to(base).as_posix()


def walk(arg: Path, root: Path) -> list[tuple[Path, str]]:
    """Файлы под аргументом и их путь для показа."""
    files = sorted(p for p in arg.rglob("*") if p.is_file()) if arg.is_dir() else [arg]
    return [(p, shown_path(p, arg, root)) for p in files]


def gitleaks_findings(path: Path, root: Path) -> list[str]:
    binary = find()
    if binary is None:
        return [f"✗ {path}: {NO_GITLEAKS}"]
    command = [str(binary), "dir", str(path), "--no-banner", "--redact", "--exit-code", "0",
               "--max-archive-depth", "2", "--report-format", "json", "--report-path", "-"]
    if (root / CONFIG).exists():
        command += ["-c", str(root / CONFIG)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    try:
        leaks = json.loads(done.stdout or "[]")
    except ValueError:
        return [f"✗ {path}: gitleaks вернул нечитаемый отчёт, код {done.returncode}"]
    return [f"✗ {leak['File']}: gitleaks {leak['RuleID']}, строка {leak['StartLine']}"
            for leak in leaks]


def check(paths: list[Path], root: Path, use_gitleaks: bool = True,
          profile: Profile = OUTSIDE) -> tuple[list[str], list[str]]:
    """Находки и список «смотреть глазами» для переданных путей."""
    patterns, env = text_patterns(root, profile), env_values(root)
    bad, eyes = [], []
    for arg in paths:
        for file, shown in walk(arg, root):
            found, look = check_file(file, shown, patterns, env)
            bad += found
            if look:
                eyes.append(shown)
        if use_gitleaks:
            bad += gitleaks_findings(arg, root)
    return bad, eyes


def verdict(bad: list[str], eyes: list[str]) -> int:
    for line in dict.fromkeys(bad):
        print(line)
    for path in eyes:
        print(f"смотреть глазами: {path}")
    count = len(set(bad))
    print(f"НЕ передавать: {count} находок" if count else "можно передавать")
    return 1 if count else 0


def make_docx(path: Path, body: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:p>{body}</w:p></w:document>")


def selftest_cases(tmp: Path, with_gitleaks: bool,
                   env_value: str) -> list[tuple[str, str, str]]:
    """(файл, ожидаемый кусок находки или «», пояснение) — файлы создаются в tmp/data."""
    data = tmp / "data"
    data.mkdir()
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n"
    texts = {
        "clean.txt": "Доверенность № 12 от 10.11.2026, водитель Иванов И. И., госномер А123ВС77",
        "task.md": "см. ab#12", "app.py": "print(1)\n",
        "letter.txt": f"пароль от кабинета {env_value}", "key.pem": pem,
    }
    for name, text in texts.items():
        (data / name).write_text(text, encoding="utf-8")
    make_docx(data / "doc.docx", "<w:t>password = </w:t><w:t>hunter2secret</w:t>")
    (data / "shot.png").write_bytes(b"\x89PNG\0\1\2")
    cases = [("clean.txt", "", "чистый текст"), ("task.md", "ссылка на задачу", "задача"),
             ("app.py", "исходник", "исходник"),
             ("letter.txt", "значение API_TOKEN из .env", "значение из .env"),
             ("doc.docx", "doc.docx!word/document.xml: пароль", "секрет в архиве"),
             ("key.pem", "закрытый ключ", "закрытый ключ"), ("shot.png", "", "картинка")]
    if with_gitleaks:
        alphabet = string.ascii_letters + string.digits
        token = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
        (data / "gh.txt").write_text(f'GITHUB_TOKEN = "{token}"\n', encoding="utf-8")
        cases.append(("gh.txt", "gitleaks", "ключ GitHub для gitleaks"))
    return cases


def outside_case(use_gitleaks: bool, root: Path) -> list[str]:
    """Служебная папка выше переданного (`~/.claude/…`) — не находка."""
    with tempfile.TemporaryDirectory() as tmp:
        report_file = Path(tmp) / ".claude" / "out" / "report.txt"
        report_file.parent.mkdir(parents=True)
        report_file.write_text("Доверенность № 12", encoding="utf-8")
        found, _ = check([report_file], root, use_gitleaks=use_gitleaks)
    return [f"служебная папка выше переданного: ждали «нет находок», вышло {found}"
            ] if found else []


def profile_cases(root: Path) -> list[str]:
    """Профиль снимает только названное внутреннее, добавляет своё; ключ не разрешить."""
    data = root / "data"
    profile = Profile(allow=frozenset({"ссылка на задачу"}),
                      extra=(("IP-адрес", r"\b10\.0\.0\.1\b"),))
    tasks, _ = check([data / "task.md"], root, use_gitleaks=False, profile=profile)
    (data / "ip.txt").write_text("сервер 10.0.0.1", encoding="utf-8")
    ip, _ = check([data / "ip.txt"], root, use_gitleaks=False, profile=profile)
    strict, _ = check([data / "ip.txt"], root, use_gitleaks=False)
    bad = [f"профиль: ссылка на задачу разрешена, вышло {tasks}"] if tasks else []
    bad += [] if any("IP-адрес" in line for line in ip) else [f"профиль: IP не пойман, {ip}"]
    bad += [f"без профиля его образцов нет, вышло {strict}"] if strict else []
    try:
        text_patterns(root, Profile(allow=frozenset({"закрытый ключ"})))
        bad.append("профиль разрешил ключ")
    except ValueError:
        pass
    return bad


def selftest() -> int:
    with_gitleaks = find() is not None
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".backlog.json").write_text('{"prefix": "ab"}', encoding="utf-8")
        # значение — на лету: записанное в коде, оно само выглядит ключом для gitleaks
        env_value = "".join(secrets.choice(string.ascii_letters) for _ in range(12))
        (root / ".env").write_text(f"API_TOKEN={env_value}\n", encoding="utf-8")
        cases = selftest_cases(root, with_gitleaks, env_value)
        for name, expected, why in cases:
            found, eyes = check([root / "data" / name], root, use_gitleaks=with_gitleaks)
            ok = (any(expected in line for line in found) if expected
                  else not found and (name != "shot.png" or eyes == [f"data/{name}"]))
            if not ok:
                bad.append(f"{why} ({name}): ждали «{expected or 'нет находок'}», вышло {found}")
        bad += outside_case(use_gitleaks=with_gitleaks, root=root)
        bad += profile_cases(root)
    if not with_gitleaks:
        print("пропущен случай gh.txt: gitleaks не найден")
    return report(bad, f"случаев {len(cases)}")


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if argv[:1] == ["--text"] and len(argv) == 2:
        with tempfile.TemporaryDirectory() as tmp:
            message = Path(tmp) / "message.txt"
            message.write_text(argv[1], encoding="utf-8")
            bad, eyes = check([message], ROOT)
        return verdict([b.replace(str(message), "--text").replace("message.txt", "--text")
                        for b in bad], eyes)
    if not argv or argv[0].startswith("-"):
        sys.exit(__doc__)
    return verdict(*check([Path(a) for a in argv], ROOT))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
