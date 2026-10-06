"""Итог самопроверки — один на все скрипты процесса, чтобы формат не расходился."""


def report(bad: list[str], passed: str) -> int:
    """Печатает упавшие проверки или строку об успехе; код возврата — 1, если что-то упало."""
    for line in bad:
        print("✗ " + line)
    if not bad:
        print("самопроверка пройдена: " + passed)
    return 1 if bad else 0
