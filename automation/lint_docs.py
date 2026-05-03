#!/usr/bin/env python3
"""
lint_docs.py — проверка качества MDX-файлов документации.

Проверки:
  1. Frontmatter: обязательные поля title и description присутствуют и не пусты.
  2. Internal links: href="/ru/..." и href="/en/..." ведут на существующие .mdx файлы.

Использование как скрипт:
    python lint_docs.py FILE [FILE ...] --repo-path PATH [--strict]

    --strict  завершить с кодом 1 даже при наличии только предупреждений

Использование как модуль:
    from lint_docs import lint_files
    errors, warnings = lint_files(paths, repo_path)
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml


# ─── Git-операции ────────────────────────────────────────────────────────────

def get_changed_ru_files(repo_path: Path, since: str) -> list[Path]:
    """Возвращает список ru/*.mdx файлов, изменённых с указанного коммита."""
    result = subprocess.run(
        ["git", "diff", "--name-only", since, "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not result.stdout:
        return []
    return [
        repo_path / line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("ru/") and line.strip().endswith(".mdx")
    ]


# ─── Парсинг frontmatter ──────────────────────────────────────────────────────

def parse_frontmatter(content: str) -> dict | None:
    """Возвращает dict frontmatter или None, если его нет / он не парсится."""
    content = content.strip()
    if not content.startswith("---"):
        return None
    end = content.find("---", 3)
    if end == -1:
        return None
    try:
        result = yaml.safe_load(content[3:end])
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None


# ─── Проверки ────────────────────────────────────────────────────────────────

def check_frontmatter(filepath: Path, content: str) -> list[str]:
    """Проверяет наличие и непустоту обязательных полей frontmatter."""
    errors = []
    fm = parse_frontmatter(content)
    if fm is None:
        errors.append(f"{filepath.name}: frontmatter отсутствует или не читается")
        return errors
    for field in ("title", "description"):
        value = fm.get(field)
        if not value or not str(value).strip():
            errors.append(f"{filepath.name}: поле '{field}' отсутствует или пустое в frontmatter")
    return errors


def check_internal_links(filepath: Path, content: str, repo_path: Path) -> list[str]:
    """
    Проверяет внутренние ссылки вида href="/ru/page" и href="/en/page".
    Ожидаемые файлы: {repo_path}/ru/page.mdx и {repo_path}/en/page.mdx.
    """
    warnings = []
    pattern = re.compile(r'href=["\'](?P<link>/(?:ru|en)/[^"\'#?\s]+)["\']')
    for match in pattern.finditer(content):
        link = match.group("link").rstrip("/")
        candidate = repo_path / (link.lstrip("/") + ".mdx")
        if not candidate.exists():
            warnings.append(
                f"{filepath.name}: битая ссылка '{link}' "
                f"(файл {candidate.relative_to(repo_path)} не найден)"
            )
    return warnings


# ─── Основная функция ────────────────────────────────────────────────────────

def lint_files(
    files: list[Path],
    repo_path: Path,
) -> tuple[list[str], list[str]]:
    """
    Проверяет список MDX-файлов.

    Возвращает (errors, warnings):
      errors   — блокирующие проблемы (отсутствие frontmatter-полей)
      warnings — некритичные проблемы (битые ссылки)
    """
    errors: list[str] = []
    warnings: list[str] = []

    for f in files:
        if not f.exists():
            warnings.append(f"{f.name}: файл не найден, пропускаю")
            continue
        content = f.read_text(encoding="utf-8")
        errors.extend(check_frontmatter(f, content))
        warnings.extend(check_internal_links(f, content, repo_path))

    return errors, warnings


def print_lint_report(errors: list[str], warnings: list[str], label: str = "") -> None:
    """Выводит отчёт линтера в stdout."""
    prefix = f"[lint{' ' + label if label else ''}]"
    if not errors and not warnings:
        print(f"{prefix} OK — проблем не найдено")
        return
    for w in warnings:
        print(f"{prefix} WARN  {w}")
    for e in errors:
        print(f"{prefix} ERROR {e}")
    counts = []
    if errors:
        counts.append(f"{len(errors)} ошибок")
    if warnings:
        counts.append(f"{len(warnings)} предупреждений")
    print(f"{prefix} Итого: {', '.join(counts)}")


# ─── CLI ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Линтер MDX-файлов документации")
    parser.add_argument(
        "files", nargs="*",
        help="Файлы для проверки (.mdx). Если не указаны, используется --since.",
    )
    parser.add_argument(
        "--repo-path", default=".",
        help="Корень репозитория mintlify-docs (для проверки ссылок)",
    )
    parser.add_argument(
        "--since", default=None,
        help="Коммит, с которого искать изменённые ru/*.mdx файлы (например HEAD~1)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Выйти с кодом 1 при наличии любых предупреждений",
    )
    args = parser.parse_args()

    repo_path = Path(args.repo_path).resolve()

    if args.files:
        # Явно переданные файлы — резолвим относительно repo_path
        files = [repo_path / f for f in args.files]
    elif args.since:
        # Автоматически определяем изменённые RU-файлы через git diff
        files = get_changed_ru_files(repo_path, args.since)
        if not files:
            print(f"[lint] Нет изменённых ru/*.mdx файлов с {args.since}")
            return
        print(f"[lint] Проверяю файлы: {', '.join(f.name for f in files)}")
    else:
        parser.error("Укажите файлы или параметр --since")

    errors, warnings = lint_files(files, repo_path)
    print_lint_report(errors, warnings)

    if errors or (args.strict and warnings):
        sys.exit(1)


if __name__ == "__main__":
    main()
